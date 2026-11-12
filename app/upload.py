"""Validate and save a batch from Streamlit's file uploader."""
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from . import db

MIME_BY_EXTENSION = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def upload_root() -> Path:
    value = Path(os.getenv("UPLOAD_DIR", "data/runtime/uploads"))
    return value if value.is_absolute() else db.ROOT / value


def content_ok(data: bytes, suffix: str) -> bool:
    if suffix == ".docx":
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                return ("word/document.xml" in names and len(names) < 2000 and
                        sum(info.file_size for info in archive.infolist()) <= 100 * 1024 * 1024 and
                        not any(info.flag_bits & 1 for info in archive.infolist()))
        except BadZipFile:
            return False
    return ((suffix == ".pdf" and data.startswith(b"%PDF-")) or
            (suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or
            (suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff")))


def save_documents(uploads: list, submission_id: str | None = None, *,
                   replaced_file_ids: list[str] | None = None,
                   expected_version: int | None = None) -> tuple[str, int]:
    """Submit documents without requiring vendor identity, contact or declaration fields."""
    return save_batch({"_superseded_file_ids": replaced_file_ids or []}, uploads,
                      submission_id, expected_version=expected_version)


def save_batch(form: dict, uploads: list, submission_id: str | None = None,
               *, expected_version: int | None = None) -> tuple[str, int]:
    """Compatibility entry point; all manual vendor fields are discarded."""
    if not os.getenv("GROQ_API_KEY"):
        raise ValueError("Set GROQ_API_KEY in .env before submitting")
    with (db.ROOT / "data/requirements.json").open(encoding="utf-8") as file:
        max_files = json.load(file)["max_files"]
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        raise ValueError("Configure max_files as a positive integer in requirements.json")
    if (not uploads and not submission_id) or len(uploads) > max_files:
        raise ValueError(f"Upload between 1 and {max_files} files")
    if submission_id and (Path(submission_id).name != submission_id or
                          submission_id in {".", ".."} or "/" in submission_id or "\\" in submission_id):
        raise ValueError("Invalid submission ID")
    previous = db.get_version(submission_id) if submission_id else None
    if submission_id and not previous:
        raise ValueError("Submission for resubmission was not found")
    if previous and expected_version is not None and previous["version"] != expected_version:
        raise ValueError("A newer submission version exists; reopen the correction form")
    superseded = form.get("_superseded_file_ids", [])
    if not isinstance(superseded, list) or any(not isinstance(value, str) for value in superseded):
        raise ValueError("Invalid selection of replaced files")
    previous_files = {file["id"] for file in previous["files"]} if previous else set()
    previous_form = previous["form"] if previous else {}
    superseded = set(superseded) | set(previous_form.get("_superseded_file_ids") or [])
    if not superseded <= previous_files:
        raise ValueError("Replaced files must belong to this submission's earlier versions")
    if not uploads and not previous_files - superseded:
        raise ValueError("At least one active supporting file is required")
    form = {"_workflow": "document_only", "_superseded_file_ids": sorted(superseded)}
    file_limit = int(os.getenv("MAX_FILE_MB", "15")) * 1024 * 1024
    batch_limit = int(os.getenv("MAX_BATCH_MB", "45")) * 1024 * 1024
    if file_limit <= 0 or batch_limit <= 0:
        raise ValueError("MAX_FILE_MB and MAX_BATCH_MB must be positive integers")
    parsed = []
    total = 0
    for uploaded in uploads:
        original = Path(uploaded.name.replace("\\", "/")).name
        suffix = Path(original).suffix.lower()
        if suffix not in MIME_BY_EXTENSION:
            raise ValueError(f"Unsupported file type: {original}")
        data = uploaded.getvalue()
        if not data or len(data) > file_limit:
            raise ValueError(f"File size invalid: {original}")
        total += len(data)
        if total > batch_limit:
            raise ValueError("Upload batch exceeds the configured size limit")
        if not content_ok(data, suffix):
            raise ValueError(f"File signature does not match its extension: {original}")
        parsed.append((original, suffix, data))
    submission_id = submission_id or str(uuid.uuid4())
    directory = upload_root() / submission_id
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    written = []
    try:
        for original, suffix, data in parsed:
            file_id = str(uuid.uuid4())
            stored_name = file_id + suffix
            destination = directory / stored_name
            written.append(destination)
            destination.write_bytes(data)
            records.append({"id": file_id, "submission_id": submission_id, "version": 0,
                            "original_name": original, "stored_name": stored_name,
                            "mime": MIME_BY_EXTENSION[suffix], "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
        version = db.create_version(submission_id, form, records,
                                    expected_version=previous["version"] if previous else 0)
    except Exception:
        for destination in written:
            destination.unlink(missing_ok=True)
        raise
    return submission_id, version
