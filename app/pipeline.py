"""Persist visible stages and process all files through the current version."""
import json
import logging
from pathlib import Path

from . import db
from .documents import read_file
from .llm import draft_followup, extract_block
from .profiles import assess_profile, load_profiles
from .rules import evaluate, summarize_documents

LOG = logging.getLogger(__name__)


def run(submission_id: str, version: int, upload_dir: str, on_stage=None) -> None:
    def emit(stage: str, message: str) -> None:
        db.event(submission_id, version, stage, message)
        if on_stage:
            try:
                on_stage(stage, message)
            except Exception:
                LOG.warning("Could not refresh progress for %s version %s", submission_id, version, exc_info=True)

    try:
        state = db.get_version(submission_id, version)
        if not state:
            return
        with db.connect() as connection:
            claimed = connection.execute("""UPDATE versions SET stage='reading'
                WHERE submission_id=? AND version=? AND stage='queued'""", (submission_id, version))
            if claimed.rowcount != 1:
                return
        superseded = set(state["form"].get("_superseded_file_ids") or [])
        root = Path(upload_dir).resolve()
        directory = (root / submission_id).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("Submission upload path is invalid")
        emit("reading", "Reading uploaded files and pages")
        blocks = []
        errors = []
        for file in db.files_through_version(submission_id, version):
            if file["id"] in superseded:
                continue
            try:
                location = (directory / file["stored_name"]).resolve()
                if not location.is_relative_to(directory):
                    raise ValueError("Stored file path is invalid")
                file_blocks = read_file(location, file["id"], file["mime"])
                if not file_blocks or not any(block["text"].strip() for block in file_blocks):
                    raise ValueError("No readable document text was found")
                for block in file_blocks:
                    block["version"] = file["version"]
                blocks.extend(file_blocks)
            except Exception as exc:
                LOG.warning("Reading failed for file %s", file["id"], exc_info=True)
                reason = str(exc) if isinstance(exc, ValueError) else "Reading failed; retry or request manual review"
                errors.append(f"{file['original_name']} (v{file['version']}): {reason}")
        emit("classifying", f"Classifying evidence from {len(blocks)} pages or images")
        emit("extracting", "Extracting source-linked fields with Groq")
        profiles = load_profiles()
        documents = []
        for block in blocks:
            try:
                extracted = extract_block(block, profiles)
                for document in extracted:
                    document["source"]["version"] = block["version"]
                    document["profile"] = assess_profile(document["document_type"], block["text"], profiles)
                documents.extend(extracted)
            except Exception as exc:
                LOG.warning("Extraction failed for file %s", block["file_id"], exc_info=True)
                reason = str(exc) if isinstance(exc, ValueError) else "Extraction failed; retry or request manual review"
                errors.append(f"File {block['file_id']}, section {block['page']} (v{block['version']}): {reason}")
        # Earlier evidence is replaced only when the operator explicitly selects
        # its file. Merely adding a new document must not hide an old conflict.
        for document in documents:
            document["active"] = True
        if version > 1:
            previous = db.get_version(submission_id, version - 1)
            for document in ((previous or {}).get("extracted") or {}).get("documents", []):
                if document["source"]["file_id"] in superseded:
                    documents.append({**document, "active": False})
        active_documents = [document for document in documents if document["active"]]
        emit("checking", "Matching document identifiers and comparing evidence with the vendor master")
        with (db.ROOT / "data/requirements.json").open(encoding="utf-8") as fh:
            policy = json.load(fh)
        vendors = db.all_vendors()
        summary = summarize_documents(active_documents, vendors)
        status, checks = evaluate({}, active_documents, vendors, policy, errors, profiles)
        emit("deciding", f"Applying policy: {status}")
        emit("drafting", "Preparing the follow-up message")
        followup = draft_followup(status, checks, summary["vendor"].get("contact_name") or summary["vendor"].get("legal_name", ""))
        db.finish(submission_id, version, status,
                  {"workflow": "document_only", **summary, "documents": documents,
                   "read_errors": errors, "block_count": len(blocks),
                   "requirements_snapshot": policy, "profiles_snapshot": profiles},
                  checks, followup)
    except Exception:
        LOG.exception("Submission %s version %s failed", submission_id, version)
        db.fail(submission_id, version, "Unexpected processing error. Check the server log and retry.")
