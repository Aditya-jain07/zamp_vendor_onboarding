"""Recreate the five synthetic baseline vendors in data/vendor_master.csv.

This script is separate from make_dataset.py so ordinary fixture generation never
overwrites a vendor master that a reviewer has edited.
"""
import argparse
from pathlib import Path

if __package__:
    from .import_vendors import write_csv_atomic
else:
    from import_vendors import write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data/vendor_master.csv"
COLUMNS = ["vendor_id", "legal_name", "vendor_type", "pan", "gstin", "account_holder",
           "bank_account", "ifsc", "address", "status", "version"]

SEED_VENDORS = [
    ["V100", "Northstar Office Supplies Private Limited", "company", "AAACN1234A",
     "27AAACN1234A1Z5", "Northstar Office Supplies Private Limited", "50300011223344",
     "HDFC0001234", "14 Market Road Mumbai Maharashtra", "approved", "1"],
    ["V101", "Meera Rao", "sole_proprietor", "AAAPR1234F", "", "Meera Rao",
     "782010099887", "SBIN0004321", "8 Lake View Pune Maharashtra", "approved", "1"],
    ["V102", "Blocked Demo Trading", "company", "AAACB9999C", "27AAACB9999C1Z2",
     "Blocked Demo Trading", "220011009900", "ICIC0000222",
     "4 Old Street Mumbai Maharashtra", "blocked", "1"],
    ["V103", "Seabrook Instruments Private Limited", "company", "AAACS4321H",
     "27AAACS4321H1Z5", "Seabrook Instruments Private Limited", "610004567890",
     "HDFC0008765", "22 Factory Lane Pune Maharashtra", "approved", "1"],
    ["V104", "Rohan Shah", "sole_proprietor", "AAAPS4321K", "", "Rohan Shah",
     "706001234567", "SBIN0007654", "9 Station Road Ahmedabad Gujarat", "approved", "1"],
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Destination CSV; defaults to data/vendor_master.csv")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an existing CSV, including reviewer edits")
    args = parser.parse_args()
    target = args.output.resolve()
    if target.exists() and not args.overwrite:
        raise SystemExit(f"{target} already exists. Use --overwrite only to reset it to defaults.")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(target, COLUMNS, SEED_VENDORS, overwrite=args.overwrite)
    print(f"Wrote {len(SEED_VENDORS)} synthetic vendors to {target}")


if __name__ == "__main__":
    main()
