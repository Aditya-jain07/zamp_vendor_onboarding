"""Recreate the editable default document-profile spreadsheet."""
import argparse
from pathlib import Path

if __package__:
    from .import_vendors import write_csv_atomic
else:
    from import_vendors import write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data/document_profiles.csv"
COLUMNS = ["document_type", "display_name", "title_cues", "field_labels", "required_fields"]

# Edit these rows to change the default headings, labels, and completeness rules.
SEED_PROFILES = [
    ["pan", "PAN proof", "PAN Card|Permanent Account Number|Tax Identifier Summary",
     "Name|PAN|Taxpayer", "pan|legal_name"],
    ["gst", "GST registration", "GST Registration|Goods and Services Tax|GSTIN Certificate",
     "Legal Name|GSTIN|Principal Place", "gstin|legal_name"],
    ["bank_proof", "Bank proof", "Account Confirmation|Cancelled Cheque|Bank Statement",
     "Account Holder|Account Number|IFSC", "account_holder|bank_account|ifsc"],
    ["business_registration", "Business registration",
     "Certificate of Incorporation|Business Registration|Entity Registration",
     "Registered Legal Name|Registration Number|CIN", "legal_name|registration_number"],
    ["declaration", "Declaration", "Vendor Declaration|Authorisation Declaration",
     "Authorised|Accurate", ""],
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an existing CSV, including any reviewer edits")
    args = parser.parse_args()
    target = args.output.resolve()
    if target.exists() and not args.overwrite:
        raise SystemExit(f"{target} already exists. Use --overwrite only to reset it to defaults.")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(target, COLUMNS, SEED_PROFILES, overwrite=args.overwrite)
    print(f"Wrote {len(SEED_PROFILES)} document profiles to {target}")


if __name__ == "__main__":
    main()
