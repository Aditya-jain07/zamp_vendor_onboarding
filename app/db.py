"""SQLite submission/audit history plus the live CSV vendor master."""
import csv
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path() -> Path:
    value = os.getenv("DATABASE_PATH", "data/runtime/onboarding.sqlite3")
    result = Path(value)
    return result if result.is_absolute() else ROOT / result


@contextmanager
def connect():
    location = path()
    location.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(location, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS submissions (
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          latest_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS versions (
          submission_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
          stage TEXT NOT NULL, status TEXT NOT NULL, form_json TEXT NOT NULL,
          extracted_json TEXT, checks_json TEXT, followup TEXT, error TEXT,
          PRIMARY KEY(submission_id,version),
          FOREIGN KEY(submission_id) REFERENCES submissions(id)
        );
        CREATE TABLE IF NOT EXISTS files (
          id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, version INTEGER NOT NULL,
          original_name TEXT NOT NULL, stored_name TEXT NOT NULL, mime TEXT NOT NULL,
          size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
          FOREIGN KEY(submission_id,version) REFERENCES versions(submission_id,version)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT NOT NULL,
          version INTEGER NOT NULL, at TEXT NOT NULL, stage TEXT NOT NULL,
          message TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_versions_status ON versions(status);
        CREATE INDEX IF NOT EXISTS idx_events_sub ON events(submission_id,version);
        """)


def create_version(submission_id: str, form: dict, files: list[dict],
                   *, expected_version: int | None = None) -> int:
    stamp = now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT latest_version FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if expected_version is not None and (existing[0] if existing else 0) != expected_version:
            raise ValueError("A newer submission version exists; reopen the correction form")
        if existing:
            version = existing[0] + 1
            db.execute("UPDATE submissions SET latest_version=?,updated_at=? WHERE id=?", (version, stamp, submission_id))
        else:
            version = 1
            db.execute("INSERT INTO submissions VALUES (?,?,?,?)", (submission_id, stamp, stamp, version))
        db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (submission_id, version, stamp, "queued", "processing", json.dumps(form), None, None, None, None))
        for item in files:
            item["version"] = version
            db.execute("""INSERT INTO files VALUES (:id,:submission_id,:version,:original_name,
                       :stored_name,:mime,:size_bytes,:sha256)""", item)
        db.execute("INSERT INTO events(submission_id,version,at,stage,message) VALUES (?,?,?,?,?)",
                   (submission_id, version, stamp, "queued", "Submission accepted"))
    return version


def event(submission_id: str, version: int, stage: str, message: str) -> None:
    with connect() as db:
        db.execute("UPDATE versions SET stage=? WHERE submission_id=? AND version=?", (stage, submission_id, version))
        db.execute("INSERT INTO events(submission_id,version,at,stage,message) VALUES (?,?,?,?,?)",
                   (submission_id, version, now(), stage, message))


def finish(submission_id: str, version: int, status: str, extracted: dict, checks: list[dict], followup: str) -> None:
    with connect() as db:
        db.execute("UPDATE submissions SET updated_at=? WHERE id=?", (now(), submission_id))
        db.execute("""UPDATE versions SET stage='complete',status=?,extracted_json=?,checks_json=?,followup=?
                      WHERE submission_id=? AND version=?""",
                   (status, json.dumps(extracted), json.dumps(checks), followup, submission_id, version))
        db.execute("INSERT INTO events(submission_id,version,at,stage,message) VALUES (?,?,?,?,?)",
                   (submission_id, version, now(), "complete", f"Decision: {status}"))


def fail(submission_id: str, version: int, error: str) -> None:
    with connect() as db:
        db.execute("UPDATE submissions SET updated_at=? WHERE id=?", (now(), submission_id))
        db.execute("UPDATE versions SET stage='failed',status='pending',error=? WHERE submission_id=? AND version=?",
                   (error[:500], submission_id, version))
        db.execute("INSERT INTO events(submission_id,version,at,stage,message) VALUES (?,?,?,?,?)",
                   (submission_id, version, now(), "failed", "Processing stopped; manual review required"))


def get_version(submission_id: str, version: int | None = None) -> dict | None:
    with connect() as db:
        if version is None:
            row = db.execute("""SELECT v.* FROM versions v JOIN submissions s ON s.id=v.submission_id
                WHERE v.submission_id=? AND v.version=s.latest_version""", (submission_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM versions WHERE submission_id=? AND version=?", (submission_id, version)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("form_json", "extracted_json", "checks_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key)) if result[key] else None
        result["metadata"] = dict(result["form"] or {})
        extracted = result.get("extracted") or {}
        result["workflow"] = extracted.get("workflow") or result["metadata"].get("_workflow", "legacy_form")
        result["vendor"] = (extracted.get("vendor") or {}) if result["workflow"] == "document_only" else dict(result["metadata"])
        result["files"] = [dict(r) for r in db.execute("SELECT * FROM files WHERE submission_id=? AND version<=? ORDER BY version,id", (submission_id, result["version"]))]
        result["events"] = [dict(r) for r in db.execute("SELECT at,stage,message FROM events WHERE submission_id=? AND version=? ORDER BY id", (submission_id, result["version"]))]
        return result


def _submission_flag(checks: list[dict], status: str, stage: str) -> str:
    """Show the issue to resolve next, rather than hiding it behind a later review."""
    issues = [check for check in checks if check.get("result") != "pass"]
    rejected = next((check for check in issues if check.get("result") == "reject"), None)
    if rejected:
        return rejected["message"]
    if stage == "failed":
        return "Processing failed; review or retry required"
    missing_documents = []
    for check in issues:
        code = check.get("code", "")
        if code.startswith("missing_") and not code.startswith("missing_field_"):
            missing_documents.append(code.removeprefix("missing_").replace("_", " "))
        elif code == "gst_reason":
            missing_documents.append("GST certificate or non-registration explanation")
    if missing_documents:
        return "Missing documents: " + ", ".join(dict.fromkeys(missing_documents))
    missing_fields = [check["code"].removeprefix("missing_field_").replace("_", " ")
                      for check in issues if check.get("code", "").startswith("missing_field_")]
    if missing_fields:
        return "Missing information: " + ", ".join(dict.fromkeys(missing_fields))
    unresolved = next((check for check in issues if check.get("code") != "new_vendor_verification"), None)
    if unresolved:
        return unresolved["message"]
    if any(check.get("code") == "new_vendor_verification" for check in issues):
        return "New vendor: procurement verification required"
    return "Review in progress" if status == "processing" else ""


def _review_summary(row: sqlite3.Row) -> dict:
    """Use the identity and decision saved for this particular review attempt."""
    item = dict(row)
    metadata = json.loads(item.pop("form_json") or "{}")
    extracted = json.loads(item.pop("extracted_json") or "{}")
    workflow = extracted.get("workflow") or metadata.get("_workflow")
    identity = extracted.get("vendor", {}) if workflow == "document_only" else metadata
    item["legal_name"] = identity.get("legal_name") or "Unidentified vendor"
    item["matched_vendor_id"] = extracted.get("match", {}).get("vendor_id") or ""
    checks = json.loads(item.pop("checks_json") or "[]")
    item["flag"] = _submission_flag(checks, item["status"], item["stage"])
    return item


def list_review_history(search: str = "", status: str = "") -> list[dict]:
    """Return attempts for the 200 most recently updated submissions in one query.

    Keep each attempt's original status; a later approval must not rewrite an
    earlier pending review in the sidebar. Filtering happens after retrieval,
    matching list_submissions' existing search behavior.
    """
    with connect() as db:
        rows = db.execute("""WITH recent AS (
            SELECT id,updated_at FROM submissions
            ORDER BY updated_at DESC,id LIMIT 200
          )
          SELECT s.id,s.updated_at,v.version,v.status,v.stage,
                 v.form_json,v.extracted_json,v.checks_json
          FROM recent s JOIN versions v ON v.submission_id=s.id
          ORDER BY s.updated_at DESC,s.id,v.version DESC""").fetchall()
    result = []
    for row in rows:
        item = _review_summary(row)
        if status and item["status"] != status:
            continue
        searchable = " ".join((item["id"], item["legal_name"], item["matched_vendor_id"]))
        if search and search.casefold() not in searchable.casefold():
            continue
        result.append(item)
    return result


def list_submissions(search: str = "", status: str = "") -> list[dict]:
    with connect() as db:
        rows = db.execute("""SELECT s.id,s.updated_at,v.version,v.status,v.stage,v.form_json,v.extracted_json,v.checks_json
          FROM submissions s JOIN versions v ON v.submission_id=s.id AND v.version=s.latest_version
          ORDER BY s.updated_at DESC LIMIT 200""").fetchall()
    result = []
    for row in rows:
        item = _review_summary(row)
        if status and item["status"] != status:
            continue
        if search and search.lower() not in (item["id"] + " " + item["legal_name"] + " " + item["matched_vendor_id"]).lower():
            continue
        result.append(item)
    return result


def all_vendors() -> list[dict]:
    """The vendor-master spreadsheet is read fresh for every submission."""
    # Local import keeps the db -> rules -> profiles -> db imports acyclic at startup.
    from .rules import PAN, GSTIN, IFSC, ACCOUNT

    required = {"vendor_id", "legal_name", "vendor_type", "pan", "gstin",
                "account_holder", "bank_account", "ifsc", "address", "status", "version"}
    with (ROOT / "data/vendor_master.csv").open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not required <= set(headers):
            raise ValueError("Vendor master needs unique headings and all required columns")
        rows = list(reader)
    if not rows:
        raise ValueError("Vendor master cannot be empty")
    seen_ids, seen_pans, seen_gsts = set(), set(), set()
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("Vendor master contains a row with missing or extra cells")
        for key in required:
            row[key] = (row[key] or "").strip()
        row["pan"] = row["pan"].upper()
        row["gstin"] = row["gstin"].upper()
        row["ifsc"] = row["ifsc"].upper()
        vendor_id, pan = row["vendor_id"], row["pan"]
        if any(not row[key] for key in required - {"gstin"}):
            raise ValueError("Only GSTIN may be blank in a vendor-master record")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", vendor_id):
            raise ValueError("Vendor master has an invalid vendor ID")
        if row["vendor_type"] not in {"company", "sole_proprietor", "individual"}:
            raise ValueError("Vendor master has an unsupported vendor type")
        if not PAN.fullmatch(pan) or not IFSC.fullmatch(row["ifsc"]) or not ACCOUNT.fullmatch(row["bank_account"]):
            raise ValueError("Vendor master has an invalid PAN, IFSC or bank account format")
        gstin = row["gstin"]
        if gstin and (not GSTIN.fullmatch(gstin) or gstin[2:12] != pan or gstin in seen_gsts):
            raise ValueError("Vendor master has an invalid, inconsistent or duplicate GSTIN")
        if not re.fullmatch(r"[1-9][0-9]*", row["version"]):
            raise ValueError("Vendor master version must be a positive integer")
        if not vendor_id or not pan or vendor_id in seen_ids or pan in seen_pans:
            raise ValueError("Vendor master contains a missing or duplicate ID/PAN")
        if row["status"] not in {"approved", "blocked"}:
            raise ValueError("Vendor master has an unsupported status")
        seen_ids.add(vendor_id)
        seen_pans.add(pan)
        if gstin:
            seen_gsts.add(gstin)
    return rows


def get_file(file_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None


def files_through_version(submission_id: str, version: int) -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM files WHERE submission_id=? AND version<=? ORDER BY version,id",
            (submission_id, version))]


def update_followup(submission_id: str, version: int, body: str) -> None:
    if not body.strip():
        raise ValueError("The follow-up draft cannot be empty")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT stage,status FROM versions WHERE submission_id=? AND version=?",
                         (submission_id, version)).fetchone()
        if not row or row["stage"] != "complete" or row["status"] not in {"pending", "rejected"}:
            raise ValueError("Only a completed pending or rejected follow-up can be edited")
        latest = db.execute("SELECT latest_version FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not latest or latest[0] != version:
            raise ValueError("Only the latest submission's follow-up can be edited")
        db.execute("UPDATE versions SET followup=? WHERE submission_id=? AND version=?",
                   (body, submission_id, version))
        db.execute("INSERT INTO events(submission_id,version,at,stage,message) VALUES (?,?,?,?,?)",
                   (submission_id, version, now(), "draft_edited", "Reviewer edited follow-up draft"))


