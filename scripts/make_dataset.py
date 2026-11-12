"""Manually generate synthetic PDF, image, DOCX, and XLSX demo assets.

The app never imports this script or the expected-results answer key.
Case values are only inputs for drawing synthetic documents. No operator form
JSON files are created; the app receives the generated documents themselves.
"""
import csv
import argparse
import io
import json
import re
import os
import shutil
import tempfile
from pathlib import Path

import fitz
from docx import Document
from docx.shared import Inches, Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

if __package__:
    from .document_designs import document_values, draw_document_page
    from .import_vendors import read_rows, validate
else:
    from document_designs import document_values, draw_document_page
    from import_vendors import read_rows, validate

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data/fixtures/cases.json"
OUTPUT = ROOT / "data/generated"
FORMATS = {"pdf", "png", "jpg", "docx_text", "docx_image"}
KINDS = {"pan", "gst", "bank_proof", "business_registration", "other"}
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GST_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def vendor_values(case: dict) -> dict:
    """Build internal fixture values; these are never an operator submission form."""
    number = int(case["id"][1:])
    vendor_type = case.get("vendor_type", "company")
    pan = case["pan"]
    return {
        "vendor_type": vendor_type,
        "existing_vendor_id": case.get("existing_vendor_id", ""),
        "legal_name": case["legal_name"],
        "trading_name": case.get("trading_name", ""),
        "contact_name": case.get("contact_name", f"Demo Contact {number}"),
        "email": case.get("email", f"vendor{number:02d}@example.test"),
        "phone": case.get("phone", f"+91 90000 {number:05d}"),
        "address": case.get("address", f"{number} Demo Road, Mumbai, Maharashtra"),
        "pan": pan,
        "gstin": case.get("gstin", f"27{pan}1Z5" if vendor_type == "company" else ""),
        "gst_non_registration_reason": case.get("gst_non_registration_reason", ""),
        "account_holder": case.get("account_holder", case["legal_name"]),
        "bank_account": case.get("bank_account", f"501000{number:06d}99"),
        "ifsc": case.get("ifsc", "HDFC0001234"),
        "declaration_correct": case.get("declaration_correct", True),
        "declaration_authorized": case.get("declaration_authorized", True),
    }


def form_for(case: dict) -> dict:
    """Compatibility alias for older fixture helpers; does not create a form file."""
    return vendor_values(case)


def manifest_entry(case: dict, first_files: list[str], later_files: list[str]) -> dict:
    """List uploadable documents only, without paths to obsolete form JSON files."""
    folder = Path(case["id"])
    return {"case_id": case["id"], "title": case["title"], "workflow": "document_only",
            "files": [(folder / name).as_posix() for name in first_files],
            "resubmission_files": [(folder / name).as_posix() for name in later_files]}


def _variant(case_id: str, index: int) -> int:
    return (int(case_id[1:]) + index) % 3


def _page_values(spec: dict, form: dict, case_id: str) -> dict:
    return document_values(spec["kind"], form, case_id, spec.get("overrides"))


def _draw(pdf: canvas.Canvas, spec: dict, form: dict, case_id: str, index: int) -> None:
    draw_document_page(pdf, spec["kind"], _page_values(spec, form, case_id), case_id,
                       _variant(case_id, index), spec.get("unreadable", False))


def write_pdf(path: Path, specs: list[dict], form: dict, case_id: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for index, spec in enumerate(specs, 1):
        _draw(pdf, spec, form, case_id, index)
    pdf.save()


def image_bytes(spec: dict, form: dict, case_id: str, index: int, output_format: str) -> bytes:
    """Rasterise a designed PDF page for scanned-image test inputs."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _draw(pdf, spec, form, case_id, index)
    pdf.save()
    with fitz.open(stream=buffer.getvalue(), filetype="pdf") as source:
        pix = source[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as image:
            image = image.convert("RGB")
            output = io.BytesIO()
            if output_format == "JPEG":
                image.save(output, "JPEG", quality=84, optimize=True)
            else:
                image.save(output, "PNG", optimize=True)
            return output.getvalue()


def write_scanned_pdf(path: Path, specs: list[dict], form: dict, case_id: str) -> None:
    """A combined PDF with raster pages and no selectable document text."""
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for index, spec in enumerate(specs, 1):
        image = image_bytes(spec, form, case_id, index, "JPEG")
        pdf.drawImage(ImageReader(io.BytesIO(image)), 0, 0, width=A4[0], height=A4[1])
        pdf.showPage()
    pdf.save()


def write_docx(path: Path, spec: dict, form: dict, case_id: str, index: int) -> None:
    kind = spec["kind"]
    values = _page_values(spec, form, case_id)
    document = Document()
    document.styles["Normal"].font.name = "Aptos"
    document.styles["Normal"].font.size = Pt(10.5)
    title = {
        "pan": "PAN Card - Synthetic Sample",
        "gst": "GST Registration Summary - Synthetic",
        "bank_proof": "Account Confirmation - Demo",
        "business_registration": "Business Registration Extract - Demo",
        "other": "Purchase Invoice - Wrong Attachment",
    }[kind]
    document.add_heading(title, 0)
    document.add_paragraph(f"Case {case_id} | Synthetic demo evidence only")
    if spec["format"] == "docx_image":
        document.add_paragraph("The supporting evidence is embedded as an image below.")
        image = io.BytesIO(image_bytes(spec, form, case_id, index, "PNG"))
        document.add_picture(image, width=Inches(4.8))
    else:
        labels = {
            "pan": [("Name on PAN", "legal_name"), ("PAN", "pan")],
            "gst": [("GSTIN", "gstin"), ("Legal Name", "legal_name"), ("Principal Place", "address")],
            "bank_proof": [("Account Holder", "account_holder"), ("Account Number", "bank_account"), ("IFSC", "ifsc")],
            "business_registration": [("Registered Legal Name", "legal_name"),
                                      ("Registration Number", "registration_number"),
                                      ("Registered Address", "address")],
            "other": [("Invoice Number", "invoice_number")],
        }[kind]
        table = document.add_table(rows=0, cols=2, style="Table Grid")
        for label, key in labels:
            row = table.add_row().cells
            row[0].text = label
            row[1].text = ("[OBSCURED - request a clearer copy]" if spec.get("unreadable") and key == "bank_account"
                           else str(values[key]))
    document.add_paragraph("Not issued by a bank, tax authority, or company registry.")
    document.save(path)


def write_xlsx(source: Path, destination: Path, sheet_name: str, widths: list[int]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    with source.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.reader(file):
            sheet.append(row)
    for row in sheet:
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True)
    sheet.row_dimensions[1].height = 30
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.number_format = "@"  # Identifiers are text; leading zeros must survive Excel.
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".xlsx", delete=False) as file:
        temporary = Path(file.name)
    try:
        workbook.save(temporary)
        os.replace(temporary, destination)
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)


def make_one(folder: Path, index: int, spec: dict, form: dict, case_id: str,
             prefix: str = "v1") -> Path:
    kind, format_name = spec["kind"], spec["format"]
    if kind not in KINDS or format_name not in FORMATS:
        raise ValueError(f"Unknown document specification: {spec}")
    suffix = "docx" if format_name.startswith("docx") else format_name
    target = folder / f"{prefix}_{index:02d}_{kind}.{suffix}"
    if format_name == "pdf":
        write_pdf(target, [spec], form, case_id)
    elif format_name in {"png", "jpg"}:
        target.write_bytes(image_bytes(spec, form, case_id, index,
                                       "PNG" if format_name == "png" else "JPEG"))
    else:
        write_docx(target, spec, form, case_id, index)
    return target


def load_cases(cases_path: Path) -> list[dict]:
    """Validate fixture structure, allowing intentionally incomplete evidence."""
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.json must contain a nonempty list")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not re.fullmatch(r"C[0-9]+", case["id"]):
            raise ValueError("Case IDs must be C followed by digits")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate case IDs")
    for case in cases:
        if not isinstance(case.get("title"), str) or not case["title"].strip():
            raise ValueError(f"{case['id']} needs a nonempty title")
        if not isinstance(case.get("legal_name"), str) or not isinstance(case.get("pan"), str):
            raise ValueError(f"{case['id']} needs text legal_name and pan fields")
        if case.get("vendor_type", "company") not in {"company", "sole_proprietor", "individual"}:
            raise ValueError(f"Unsupported vendor type in {case['id']}")
        for key in ("combined_pdf", "scanned_pdf", "allow_invalid_identifiers"):
            if key in case and not isinstance(case[key], bool):
                raise ValueError(f"{case['id']}: {key} must be a boolean")
        if not isinstance(case.get("docs"), list) or not isinstance(case.get("resubmission_docs", []), list):
            raise ValueError(f"{case['id']}: document specifications must be lists")
        if not isinstance(case.get("resubmission_form", {}), dict):
            raise ValueError(f"{case['id']}: resubmission_form must be an object")
        form = vendor_values(case)
        for key, value in form.items():
            if key not in {"declaration_correct", "declaration_authorized"} and not isinstance(value, str):
                raise ValueError(f"{case['id']}: {key} must be text; preserve leading zeros")
        for key, value in case.get("resubmission_form", {}).items():
            if key not in form or (key not in {"declaration_correct", "declaration_authorized"} and not isinstance(value, str)):
                raise ValueError(f"{case['id']}: invalid resubmission field {key}")
        if case.get("resubmission_form") and not case.get("resubmission_docs"):
            raise ValueError(f"{case['id']}: changed fixture values require resubmission documents; metadata alone is not evidence")
        if not case.get("allow_invalid_identifiers", case["id"] == "C13"):
            if not PAN_PATTERN.fullmatch(form["pan"]):
                raise ValueError(f"Malformed PAN in {case['id']}")
            expected_holder = "C" if form["vendor_type"] == "company" else "P"
            if form["pan"][3] != expected_holder:
                raise ValueError(f"PAN holder type mismatch in {case['id']}")
            if form["gstin"] and (not GST_PATTERN.fullmatch(form["gstin"]) or
                                  form["gstin"][2:12] != form["pan"]):
                raise ValueError(f"Malformed GSTIN structure in {case['id']}")
        for spec in case["docs"] + case.get("resubmission_docs", []):
            if not isinstance(spec, dict) or spec.get("kind") not in KINDS or spec.get("format") not in FORMATS:
                raise ValueError(f"Unknown document spec in {case['id']}: {spec}")
            if not isinstance(spec.get("overrides", {}), dict) or any(not isinstance(value, str) for value in spec.get("overrides", {}).values()):
                raise ValueError(f"{case['id']}: document overrides must contain text values")
            if not isinstance(spec.get("unreadable", False), bool):
                raise ValueError(f"{case['id']}: unreadable must be a boolean")
            if spec.get("unreadable") and spec["kind"] != "bank_proof":
                raise ValueError(f"{case['id']}: unreadable is currently supported only for bank proof")
        if case.get("scanned_pdf") and case.get("combined_pdf"):
            raise ValueError(f"Choose either scanned_pdf or combined_pdf in {case['id']}")
        if not case["docs"]:
            raise ValueError(f"{case['id']} needs at least one uploaded document")
    return cases


def generate(cases_path: Path, output: Path, *, master_path: Path | None = None,
             profiles_path: Path | None = None) -> int:
    cases = load_cases(cases_path)
    master_path = master_path or ROOT / "data/vendor_master.csv"
    profiles_path = profiles_path or ROOT / "data/document_profiles.csv"
    validate(read_rows(master_path))
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for case in cases:
        folder = output / case["id"]
        folder.mkdir(parents=True, exist_ok=True)
        form = vendor_values(case)
        if case.get("scanned_pdf"):
            target = folder / "combined_scanned_evidence.pdf"
            write_scanned_pdf(target, case["docs"], form, case["id"])
            first_files = [target.name]
        elif case.get("combined_pdf"):
            target = folder / "combined_evidence.pdf"
            write_pdf(target, case["docs"], form, case["id"])
            first_files = [target.name]
        else:
            first_files = [make_one(folder, index, spec, form, case["id"]).name
                           for index, spec in enumerate(case["docs"], 1)]
        corrected_form = {**form, **case.get("resubmission_form", {})}
        later_files = [make_one(folder, index, spec, corrected_form, case["id"], prefix="v2").name
                       for index, spec in enumerate(case.get("resubmission_docs", []), 1)]
        manifest.append(manifest_entry(case, first_files, later_files))
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_xlsx(master_path, output / "vendor_master.xlsx", "Vendor master",
               [14, 42, 20, 18, 22, 42, 24, 18, 44, 14, 10])
    write_xlsx(profiles_path, output / "document_profiles.xlsx",
               "Document profiles", [24, 29, 68, 55, 40])
    return len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    parser.add_argument("--master", type=Path, default=ROOT / "data/vendor_master.csv")
    parser.add_argument("--profiles", type=Path, default=ROOT / "data/document_profiles.csv")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing generated dataset")
    args = parser.parse_args()
    target = args.output_root.resolve()
    if target == ROOT or target in ROOT.parents or target == ROOT / "data":
        raise ValueError("Choose a dedicated generated-output directory")
    for source in (args.cases, args.master, args.profiles):
        if source.resolve().is_relative_to(target):
            raise ValueError("Input files must be outside the generated-output directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    with lock.open("x"):
        pass
    staging = None
    backup = None
    published = False
    try:
        if target.exists() and not args.overwrite:
            raise ValueError("Output already exists; use --overwrite to regenerate it")
        if target.exists() and not (target / "manifest.json").is_file():
            raise ValueError("Refusing to replace a directory without a generated dataset manifest")
        staging = Path(tempfile.mkdtemp(prefix=target.name + ".staging-", dir=target.parent))
        count = generate(args.cases, staging, master_path=args.master, profiles_path=args.profiles)
        if target.exists():
            backup = Path(tempfile.mkdtemp(prefix=target.name + ".backup-", dir=target.parent))
            backup.rmdir()
            target.rename(backup)
        try:
            staging.rename(target)
            published = True
        except Exception:
            if backup is not None:
                try:
                    backup.rename(target)
                except OSError as exc:
                    raise RuntimeError(f"Could not restore dataset; backup retained at {backup}") from exc
                backup = None
            raise
        print(f"Created {count} synthetic cases in {target}")
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if published and backup is not None and backup.exists():
            shutil.rmtree(backup)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
