"""Convert a reviewed CSV/XLSX vendor master into the app's authoritative CSV."""
import argparse
import csv
import os
import re
import tempfile
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data/vendor_master.csv"
COLUMNS = ["vendor_id", "legal_name", "vendor_type", "pan", "gstin", "account_holder",
           "bank_account", "ifsc", "address", "status", "version"]


def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            _headers(reader.fieldnames or [])
            rows = list(reader)
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError("CSV contains a row with missing or extra cells")
            return rows
    if path.suffix.lower() == ".xlsx":
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            cells = iter(book.active.iter_rows())
            headers = [cell.value for cell in next(cells, [])]
            _headers(headers)
            rows = []
            for row in cells:
                if all(cell.value is None for cell in row):
                    continue
                if any(cell.data_type == "f" for cell in row):
                    raise ValueError("Replace spreadsheet formulas with reviewed text values before importing")
                rows.append(dict(zip(headers, [cell.value for cell in row])))
            return rows
        finally:
            book.close()
    raise ValueError("Expected CSV or XLSX")


def _headers(headers: list) -> None:
    if (any(not isinstance(header, str) or not header.strip() for header in headers) or
            len(set(headers)) != len(headers) or not set(COLUMNS) <= set(headers)):
        raise ValueError("Vendor master needs unique column headings and all required columns")


def validate(rows: list[dict]) -> list[dict]:
    if not rows:
        raise ValueError("Vendor master cannot be empty")
    seen_ids, seen_pans, seen_gsts = set(), set(), set()
    result = []
    for row in rows:
        if not set(COLUMNS) <= row.keys():
            raise ValueError("Missing required vendor-master columns")
        if any(not isinstance(row[key], str) for key in ("vendor_id", "pan", "bank_account", "ifsc")):
            raise ValueError("Vendor ID, PAN, bank account and IFSC must be stored as text in the spreadsheet")
        cleaned = {key: str(row[key] or "").strip() for key in COLUMNS}
        for key in ("pan", "gstin", "ifsc"):
            cleaned[key] = cleaned[key].upper()
        if any(not cleaned[key] for key in COLUMNS if key != "gstin"):
            raise ValueError("Only GSTIN may be blank in a vendor-master record")
        if cleaned["vendor_id"] in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_-]+", cleaned["vendor_id"]):
            raise ValueError("Vendor IDs may contain letters, digits, underscores and hyphens")
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", cleaned["vendor_id"], re.IGNORECASE):
            raise ValueError("Vendor ID is a reserved Windows filename")
        if cleaned["vendor_type"] not in {"company", "sole_proprietor", "individual"}:
            raise ValueError("Unsupported vendor type")
        if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", cleaned["pan"]):
            raise ValueError("Invalid PAN structure in vendor master")
        if not re.fullmatch(r"[0-9]{9,18}", cleaned["bank_account"]):
            raise ValueError("Bank account must contain 9 to 18 digits stored as text")
        if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
            raise ValueError("Invalid IFSC structure in vendor master")
        gstin = cleaned["gstin"]
        if gstin and (not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin)
                      or gstin[2:12] != cleaned["pan"]):
            raise ValueError("GSTIN structure or embedded PAN is inconsistent")
        if gstin and gstin in seen_gsts:
            raise ValueError("Duplicate GSTIN in vendor master")
        if not re.fullmatch(r"[1-9][0-9]*", cleaned["version"]):
            raise ValueError("Vendor version must be a positive integer")
        if cleaned["status"] not in {"approved", "blocked"}:
            raise ValueError("Status must be approved or blocked")
        if cleaned["vendor_id"].casefold() in seen_ids or cleaned["pan"] in seen_pans:
            raise ValueError("Missing or duplicate vendor ID/PAN")
        seen_ids.add(cleaned["vendor_id"].casefold())
        seen_pans.add(cleaned["pan"])
        if gstin:
            seen_gsts.add(gstin)
        result.append(cleaned)
    return result


def write_csv_atomic(target: Path, columns: list[str], rows: list, *, overwrite: bool = True) -> None:
    """Publish a complete CSV, retaining the previous file on write failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
    try:
        with temporary.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row[key] for key in columns] if isinstance(row, dict) else row)
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Reviewed CSV or XLSX vendor master")
    args = parser.parse_args()
    rows = validate(read_rows(args.source))
    write_csv_atomic(TARGET, COLUMNS, rows)
    print(f"Imported {len(rows)} vendor records into {TARGET}")


if __name__ == "__main__":
    main()
