"""Check document-only demo assets; never called by the app.

Expected field values come from fixture definitions, not generated form files.
Legacy form paths in older manifests are ignored.
"""
import argparse
import io
import json
from pathlib import Path
from zipfile import ZipFile
import math
import re

from docx import Document

if __package__:
    from .make_dataset import vendor_values, load_cases
    from .document_designs import document_values
else:
    from make_dataset import vendor_values, load_cases
    from document_designs import document_values

import pymupdf as fitz
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data/generated"
CASES = ROOT / "data/fixtures/cases.json"


def asset_path(name: str) -> Path:
    if not isinstance(name, str) or not name:
        raise ValueError("Manifest paths must be nonempty strings")
    path = (GENERATED / name.replace("\\", "/")).resolve()
    if not path.is_relative_to(GENERATED.resolve()):
        raise ValueError("Manifest path points outside the generated directory")
    return path


def pdf_pages(path: Path, scanned: bool, expected_pages: int, errors: list[str],
              *, make_previews: bool = True, expected_text: list[tuple[dict, dict]] | None = None) -> list[Image.Image]:
    previews = []
    with fitz.open(str(path)) as pdf:
        if pdf.page_count != expected_pages:
            errors.append(f"{path}: expected {expected_pages} pages, found {pdf.page_count}")
        for number, page in enumerate(pdf, 1):
            text = page.get_text("text")
            if not scanned and "demo evidence only" not in " ".join(text.casefold().split()):
                errors.append(f"{path}, page {number}: demo evidence footer missing")
            if scanned and text.strip():
                errors.append(f"{path}, page {number}: scanned page has selectable text")
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        x0, y0, x1, y1 = span["bbox"]
                        if x0 < -1 or y0 < -1 or x1 > page.rect.width + 1 or y1 > page.rect.height + 1:
                            errors.append(f"{path}, page {number}: text outside page: {span['text'][:30]}")
            if not scanned and expected_text and number <= len(expected_text):
                spec, values = expected_text[number - 1]
                check_text(text, spec, values, f"{path}, page {number}", errors)
            if make_previews:
                scale = min(0.7, 1000 / max(1, page.rect.width, page.rect.height))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                previews.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    return previews


def check_text(text: str, spec: dict, values: dict, label: str, errors: list[str]) -> None:
    fields = {
        "pan": ["legal_name", "pan"], "gst": ["gstin", "legal_name", "address"],
        "bank_proof": ["account_holder", "bank_account", "ifsc"],
        "business_registration": ["legal_name", "registration_number", "address"],
        "other": ["invoice_number"],
    }[spec["kind"]]
    normalize = lambda value: re.sub(r"\s+", " ", str(value)).strip().casefold()
    actual = normalize(text)
    for key in fields:
        if spec.get("unreadable") and key == "bank_account":
            if "obscured" not in actual or (normalize(values[key]) and normalize(values[key]) in actual):
                errors.append(f"{label}: obscured bank account is missing its marker or leaks the original value")
            continue
        if not values[key]:  # Empty fixture values can be deliberate negative cases.
            continue
        expected = normalize(values[key] or "N/A")
        if expected not in actual:
            errors.append(f"{label}: expected {key} value is missing or altered")


def expected_assets(specs: dict) -> tuple[list[tuple], list[tuple]]:
    case_id = specs["id"]
    form = vendor_values(specs)
    def separate(documents, current_form, prefix):
        return [(f"{case_id}/{prefix}_{index:02d}_{spec['kind']}." +
                 ("docx" if spec["format"].startswith("docx") else spec["format"]),
                 [spec], current_form, False)
                for index, spec in enumerate(documents, 1)]
    if specs.get("combined_pdf") or specs.get("scanned_pdf"):
        scanned = bool(specs.get("scanned_pdf"))
        name = "combined_scanned_evidence.pdf" if scanned else "combined_evidence.pdf"
        first = [(f"{case_id}/{name}", specs["docs"], form, scanned)]
    else:
        first = separate(specs["docs"], form, "v1")
    later = separate(specs.get("resubmission_docs", []), {**form, **specs.get("resubmission_form", {})}, "v2")
    return first, later


def check_case(case: dict, specs: dict, errors: list[str], make_previews: bool) -> None:
    images = []
    first, later = expected_assets(specs)
    if case.get("files") != [entry[0] for entry in first] or case.get("resubmission_files", []) != [entry[0] for entry in later]:
        errors.append(f"{case['case_id']}: manifest paths disagree with the case definition")
    for name, document_specs, form, scanned in first + later:
        try:
            path = asset_path(name)
            if not path.is_file():
                errors.append(f"Missing file: {path}")
                continue
            values = [(spec, document_values(spec["kind"], form, specs["id"], spec.get("overrides")))
                      for spec in document_specs]
            if path.suffix.lower() == ".pdf":
                images.extend(pdf_pages(path, scanned, len(document_specs), errors,
                                        make_previews=make_previews, expected_text=values))
            elif path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                with Image.open(path) as image:
                    expected_format = "PNG" if path.suffix.lower() == ".png" else "JPEG"
                    if image.format != expected_format:
                        errors.append(f"{path}: image encoding does not match extension")
                    image.verify()
                with Image.open(path) as image:
                    image.load()
                    if make_previews:
                        images.append(ImageOps.exif_transpose(image).convert("RGB").copy())
            elif path.suffix.lower() == ".docx":
                document = Document(path)  # Parse the actual package and XML, not only the ZIP names.
                text = "\n".join([p.text for p in document.paragraphs] +
                                  [cell.text for table in document.tables for row in table.rows for cell in row.cells])
                if "Synthetic demo evidence only" not in text:
                    errors.append(f"{path}: demo label missing")
                if document_specs[0]["format"] == "docx_text":
                    check_text(text, *values[0], str(path), errors)
                with ZipFile(path) as archive:
                    media = [name for name in archive.namelist() if name.startswith("word/media/") and not name.endswith("/")]
                    if document_specs[0]["format"] == "docx_image" and not media:
                        errors.append(f"{path}: expected embedded evidence image is missing")
                    for name in media:
                        with Image.open(io.BytesIO(archive.read(name))) as image:
                            image.load()
                            if make_previews:
                                images.append(image.convert("RGB").copy())
            else:
                errors.append(f"Unexpected file type: {path}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    if make_previews and images:
        width = 310
        thumbs = []
        for image in images:
            image.thumbnail((width, 425))
            tile = Image.new("RGB", (width, 455), "white")
            tile.paste(image, ((width - image.width) // 2, 15))
            thumbs.append(tile)
        columns = min(4, len(thumbs))
        sheet = Image.new("RGB", (width * columns, 455 * math.ceil(len(thumbs) / columns)), "#EFF2F6")
        for index, tile in enumerate(thumbs):
            sheet.paste(tile, (width * (index % columns), 455 * (index // columns)))
        preview_dir = GENERATED / "_previews"
        preview_dir.mkdir(exist_ok=True)
        sheet.save(preview_dir / f"{case['case_id']}.png")


def main() -> None:
    global GENERATED, CASES
    parser = argparse.ArgumentParser()
    parser.add_argument("--previews", action="store_true", help="Write per-case contact sheets for visual review")
    parser.add_argument("--generated", type=Path, default=GENERATED)
    parser.add_argument("--cases", type=Path, default=CASES)
    args = parser.parse_args()
    GENERATED, CASES = args.generated.resolve(), args.cases.resolve()
    manifest_path = GENERATED / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("Run make_dataset.py yourself before this checker")
    errors = []
    try:
        cases = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(cases, list):
            raise ValueError("Dataset manifest must be a list")
    except (ValueError, OSError) as exc:
        errors.append(f"Invalid dataset manifest: {exc}")
        cases = []
    try:
        definitions = {case["id"]: case for case in load_cases(CASES)}
    except (ValueError, OSError) as exc:
        errors.append(f"Invalid fixture definitions: {exc}")
        definitions = {}
    seen = set()
    for index, case in enumerate(cases, 1):
        label = f"manifest entry {index}"
        try:
            if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
                raise ValueError("Expected an object with a text case_id")
            case_id = case["case_id"]
            label = case_id
            if case_id in seen:
                errors.append(f"Duplicate manifest case: {case_id}")
            seen.add(case_id)
            specs = definitions.get(case_id)
            if specs is None:
                errors.append(f"Unknown case in manifest: {case_id}")
                continue
            if case.get("title") != specs["title"]:
                errors.append(f"{case_id}: manifest title disagrees with fixture definition")
            if case.get("workflow", "document_only") != "document_only":
                errors.append(f"{case_id}: unsupported manifest workflow")
            check_case(case, specs, errors, args.previews)
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    for case_id in definitions.keys() - seen:
        errors.append(f"Case missing from manifest: {case_id}")
    report = {"cases": len(cases), "errors": errors,
              "preview_directory": str(GENERATED / "_previews") if args.previews else None}
    (GENERATED / "layout_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Checked {len(cases)} cases; found {len(errors)} file/layout issues")
    for error in errors:
        print("-", error)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
