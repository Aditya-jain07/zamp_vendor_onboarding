"""Document characteristics loaded from the editable CSV spreadsheet."""
import csv
import re
from pathlib import Path

from . import db

PROFILE_PATH = db.ROOT / "data/document_profiles.csv"


def _parts(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split("|") if item.strip()]


def load_profiles(path: Path = PROFILE_PATH) -> dict[str, dict]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        columns = {"document_type", "display_name", "title_cues", "field_labels", "required_fields"}
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not columns <= set(headers):
            raise ValueError("Document-profile spreadsheet is missing required columns")
        rows = list(reader)
    result = {}
    for row in rows:
        if None in row or any(row[key] is None for key in columns):
            raise ValueError("Document-profile spreadsheet contains a malformed row")
        kind = row["document_type"].strip()
        if kind not in {"pan", "gst", "bank_proof", "business_registration", "declaration", "other"} or kind in result:
            raise ValueError(f"Missing or duplicate document profile: {kind}")
        result[kind] = {
            "display_name": row["display_name"].strip(),
            "title_cues": _parts(row["title_cues"]),
            "field_labels": _parts(row["field_labels"]),
            "required_fields": _parts(row["required_fields"]),
        }
        if not result[kind]["display_name"] or not (result[kind]["title_cues"] or result[kind]["field_labels"]):
            raise ValueError(f"Document profile {kind} needs a display name and classification cues")
        fields = result[kind]["required_fields"]
        allowed = {"legal_name", "trading_name", "pan", "gstin", "account_holder", "bank_account",
                   "ifsc", "registration_number", "address"}
        if not set(fields) <= allowed or len(fields) != len(set(fields)):
            raise ValueError(f"Document profile {kind} has unknown or duplicate required fields")
        if kind in {"pan", "gst", "bank_proof", "business_registration"} and not fields:
            raise ValueError(f"Document profile {kind} must specify required fields")
    if not {"pan", "gst", "bank_proof", "business_registration"} <= result.keys():
        raise ValueError("Document-profile spreadsheet is missing a required type")
    return result


def assess_profile(kind: str, text: str, profiles: dict[str, dict]) -> dict:
    profile = profiles.get(kind)
    if not profile:
        return {"matched": kind == "other", "title_cues": [], "field_labels": []}
    lowered = " " + " ".join(re.findall(r"[^\W_]+", text.casefold())) + " "
    titles = [cue for cue in profile["title_cues"]
              if " " + " ".join(re.findall(r"[^\W_]+", cue.casefold())) + " " in lowered]
    labels = [label for label in profile["field_labels"]
              if " " + " ".join(re.findall(r"[^\W_]+", label.casefold())) + " " in lowered]
    # A profile is a flexible classification aid, not an authenticity test.
    return {"matched": bool(titles) or len(labels) >= 2,
            "title_cues": titles, "field_labels": labels}
