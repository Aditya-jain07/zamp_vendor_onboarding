"""Document-only extraction, persistence, correction and follow-up message regressions.

Network/readers are mocked; SQLite and decision rules are real. No document
generators are called, and no sample PDF/DOCX documents are produced.
"""
import copy
import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lxml import etree
from PIL import Image

from app import db, documents, llm, pipeline, profiles, upload
from app.rules import evaluate, summarize_documents

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    for name in ("vendor_master.csv", "document_profiles.csv", "requirements.json"):
        (tmp_path / "data" / name).write_bytes((ROOT / "data" / name).read_bytes())
    monkeypatch.setattr(db, "ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")
    loaded_profiles = profiles.load_profiles(tmp_path / "data/document_profiles.csv")
    monkeypatch.setattr(pipeline, "load_profiles", lambda: loaded_profiles)
    db.init()
    vendor = db.all_vendors()[0]
    vendor = {**vendor, "contact_name": "Demo", "email": "vendor@example.test", "phone": "+91 9000012345"}
    return tmp_path, vendor, loaded_profiles


def attachment():
    # Signature-only stub. Tests that process this upload always mock the reader.
    return SimpleNamespace(name="proof.pdf", getvalue=lambda: b"%PDF-1.7\ntest")


def vendor_documents(vendor, file_id="evidence"):
    keys = {"pan": ["legal_name", "pan"], "bank_proof": ["account_holder", "bank_account", "ifsc"]}
    if vendor.get("gstin"):
        keys["gst"] = ["legal_name", "gstin", "address"]
    if vendor["vendor_type"] == "company":
        keys["business_registration"] = ["legal_name", "registration_number", "address"]
    values = {"registration_number": "DEMO-REG", **vendor}
    return [{"document_type": kind, "confidence": .99, "profile": {"matched": True},
             "source": {"file_id": file_id, "page": 1, "method": "mock_text"},
             "fields": [{"key": key, "value": values[key], "quote": str(values[key]), "confidence": .99} for key in fields]}
            for kind, fields in keys.items()]


def model_result(payload):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload)))])


def extract(monkeypatch, payload):
    client = MagicMock()
    client.chat.completions.create.return_value = model_result(payload)
    monkeypatch.setattr(llm, "_client", lambda: client)
    return llm.extract_block({"text": "PAN Card PAN: AAACN1234A", "page": 1,
                              "file_id": "f", "method": "pdf_text"}, {})


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -1, 2, True, "0.99", None])
def test_invalid_model_confidence_never_becomes_reliable(monkeypatch, confidence):
    with pytest.raises(ValueError, match="confidence"):
        extract(monkeypatch, {"documents": [{"document_type": "pan", "confidence": confidence, "fields": []}]})


@pytest.mark.parametrize("payload", [[], {}, {"documents": None}, {"documents": [None]},
                                     {"documents": [{"document_type": "invented", "fields": []}]}])
def test_malformed_model_response_is_reported(monkeypatch, payload):
    with pytest.raises(ValueError):
        extract(monkeypatch, payload)


def test_unquoted_conflicting_field_is_not_silently_discarded(monkeypatch):
    result = extract(monkeypatch, {"documents": [{"document_type": "pan", "confidence": .99,
        "fields": [{"key": "pan", "value": "OTHER1234A", "quote": "PAN: OTHER1234A", "confidence": .99}]}]})
    assert result[0]["fields"] == []
    assert result[0]["validation_errors"]


def test_docx_preserves_table_order_and_reads_headers_without_generating_a_docx(monkeypatch):
    def xml(text):
        return etree.fromstring(text.encode())
    body = xml('<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Before</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Middle table</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>After</w:t></w:r></w:p></w:body>')
    def part(name, text):
        element = xml('<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:hdr>')
        return SimpleNamespace(is_linked_to_previous=False, part=SimpleNamespace(partname='/word/'+name), _element=element)
    linked = SimpleNamespace(is_linked_to_previous=True)
    section = SimpleNamespace(header=part('header1.xml','Header PAN AAACN1234A'),
        footer=part('footer1.xml','Footer registration 123'), first_page_header=linked,
        even_page_header=linked, first_page_footer=linked, even_page_footer=linked)
    doc = SimpleNamespace(element=SimpleNamespace(body=body), sections=[section])
    archive = MagicMock()
    archive.__enter__.return_value = archive
    archive.infolist.return_value = []
    monkeypatch.setattr(documents, 'ZipFile', lambda *args: archive)
    monkeypatch.setattr(documents, 'Document', lambda *args: doc)
    blocks = dict(documents.docx_text_blocks(Path('mock-only.docx')))
    assert blocks['body'].index('Before') < blocks['body'].index('Middle table') < blocks['body'].index('After')
    assert any('Header PAN' in value for value in blocks.values())
    assert any('Footer registration' in value for value in blocks.values())


def test_transparent_image_is_composited_on_white():
    source = io.BytesIO()
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(source, "PNG")
    result = Image.open(io.BytesIO(documents._jpeg(source.getvalue())))
    assert min(result.getpixel((10, 10))) > 240


def test_reprocessing_retains_files_exclusions_and_version_guard(runtime):
    sid, version = upload.save_documents([attachment(), attachment()])
    old = db.get_version(sid)
    replaced = old['files'][0]['id']
    _, second = upload.save_documents([], sid, replaced_file_ids=[replaced], expected_version=version)
    current = db.get_version(sid)
    assert second == 2 and len(current['files']) == 2
    assert current['metadata'] == {'_workflow': 'document_only', '_superseded_file_ids': [replaced]}
    assert current['vendor'] == {}
    with pytest.raises(ValueError, match='newer'):
        upload.save_documents([], sid, expected_version=version)


def test_cannot_remove_all_evidence_without_replacement(runtime):
    sid, _ = upload.save_documents([attachment()])
    file_id = db.get_version(sid)["files"][0]["id"]
    with pytest.raises(ValueError, match="active supporting"):
        upload.save_documents([], sid, replaced_file_ids=[file_id])


def test_atomic_version_guard_rejects_stale_writer(runtime):
    _, vendor, _ = runtime
    sid, _ = upload.save_documents([attachment()])
    db.create_version(sid, {"_workflow": "document_only"}, [], expected_version=1)
    with pytest.raises(ValueError, match="newer"):
        db.create_version(sid, {"_workflow": "document_only"}, [], expected_version=1)
    assert db.get_version(sid)["version"] == 2


@pytest.mark.parametrize("change", ["empty", "duplicate_heading", "extra_cell", "invalid_account", "invalid_version"])
def test_runtime_master_validation(runtime, change):
    root, _, _ = runtime
    path = root / "data/vendor_master.csv"
    with path.open(newline="") as file:
        rows = list(csv.reader(file))
    if change == "empty":
        rows = rows[:1]
    elif change == "duplicate_heading":
        rows[0].append(rows[0][0])
    elif change == "extra_cell":
        rows[1].append("unexpected")
    elif change == "invalid_account":
        rows[1][rows[0].index("bank_account")] = "1.23E+12"
    else:
        rows[1][rows[0].index("version")] = "0"
    with path.open("w", newline="") as file:
        csv.writer(file).writerows(rows)
    with pytest.raises(ValueError):
        db.all_vendors()


def ready_for_followup(vendor):
    sid, version = upload.save_documents([attachment()])
    # Simulate a completed run that extracted this contact from vendor evidence.
    extracted = {'workflow': 'document_only', 'vendor': dict(vendor)}
    db.finish(sid, version, 'pending', extracted, [], 'Please provide the missing proof.')
    return sid, version






def test_stale_or_empty_message_edit_is_blocked(runtime):
    _, vendor, _ = runtime
    sid, version = ready_for_followup(vendor)
    with pytest.raises(ValueError, match="empty"):
        db.update_followup(sid, version, "  ")
    upload.save_documents([], sid)
    with pytest.raises(ValueError, match="latest"):
        db.update_followup(sid, version, "Old draft")


def test_pipeline_keeps_unsupported_extraction_pending(runtime, monkeypatch):
    root, vendor, loaded_profiles = runtime
    sid, version = upload.save_documents([attachment()])
    evidence = []
    for kind in ("pan", "gst", "bank_proof", "business_registration"):
        fields = [{"key": key, "value": vendor.get(key, "DEMO-REG"), "confidence": .99, "quote": key}
                  for key in loaded_profiles[kind]["required_fields"]]
        evidence.append({"document_type": kind, "confidence": .99, "fields": fields,
                         "source": {"file_id": db.get_version(sid)["files"][0]["id"], "page": 1}})
    evidence[0]["validation_errors"] = ["An additional PAN was not supported by its quote"]
    text = "PAN Card GST Registration Account Confirmation Certificate of Incorporation"
    monkeypatch.setattr(pipeline, "read_file", lambda *args: [{"text": text, "file_id": "f", "page": 1}])
    monkeypatch.setattr(pipeline, "extract_block", lambda *args: copy.deepcopy(evidence))
    monkeypatch.setattr(pipeline, "draft_followup", lambda *args: "Please review.")
    pipeline.run(sid, version, str(root / "uploads"))
    result = db.get_version(sid)
    assert result["stage"] == "complete" and result["status"] == "pending"
    assert any(check["code"] == "unsupported_extraction" for check in result["checks"])


def test_gst_only_match_does_not_fill_missing_pan_from_master(runtime):
    root, vendor, loaded_profiles = runtime
    evidence = vendor_documents(vendor)
    evidence = [doc for doc in evidence if doc['document_type'] != 'pan']
    policy = json.loads((root / 'data/requirements.json').read_text())
    status, checks = evaluate({}, evidence, db.all_vendors(), policy, profiles=loaded_profiles)
    summary = summarize_documents(evidence, db.all_vendors())
    assert summary['match']['vendor_id'] == vendor['vendor_id']
    assert 'pan' not in summary['vendor']
    assert status == 'pending' and any(check['code'] == 'missing_pan' for check in checks)


@pytest.mark.parametrize("change", ["unknown_field", "empty_fields", "duplicate_heading"])
def test_profile_configuration_errors_are_reported(runtime, change):
    root, _, _ = runtime
    path = root / "data/document_profiles.csv"
    with path.open(newline="") as file:
        rows = list(csv.reader(file))
    if change == "duplicate_heading":
        rows[0].append(rows[0][0])
    else:
        rows[1][-1] = "pan_number_typo" if change == "unknown_field" else ""
    with path.open("w", newline="") as file:
        csv.writer(file).writerows(rows)
    with pytest.raises(ValueError):
        profiles.load_profiles(path)


def test_changed_master_account_holder_requires_review(runtime):
    root, vendor, loaded_profiles = runtime
    evidence = vendor_documents(vendor)
    vendors = db.all_vendors()
    vendors[0]['account_holder'] = 'Previous holder'
    policy = json.loads((root / 'data/requirements.json').read_text())
    status, checks = evaluate({}, evidence, vendors, policy, profiles=loaded_profiles)
    assert status == 'pending' and any(check['code'] == 'changed_bank' for check in checks)


def mock_processing(monkeypatch, evidence_for):
    """Use controlled read/extraction output; never open uploaded signature stubs."""
    read = MagicMock(side_effect=lambda path, file_id, mime: [{
        "file_id": file_id, "page": 1, "method": "mock_text",
        "text": "PAN Card GST Registration Account Confirmation Certificate of Incorporation"}])
    monkeypatch.setattr(pipeline, "read_file", read)
    monkeypatch.setattr(pipeline, "extract_block", lambda block, loaded: evidence_for(block["file_id"]))
    monkeypatch.setattr(pipeline, "draft_followup", lambda status, checks, name: "" if status == "approved" else "Please review the evidence.")
    return read


def test_upload_compatibility_entry_discards_manual_identity(runtime):
    _, vendor, _ = runtime
    sid, _ = upload.save_batch({**vendor, "existing_vendor_id": "V100", "declaration_correct": True}, [attachment()])
    item = db.get_version(sid)
    assert item["vendor"] == {}
    assert item["metadata"] == {"_workflow": "document_only", "_superseded_file_ids": []}


def test_pipeline_saves_automatic_match_without_a_form_and_is_idempotent(runtime, monkeypatch):
    root, vendor, _ = runtime
    sid, version = upload.save_documents([attachment()])
    read = mock_processing(monkeypatch, lambda file_id: vendor_documents(vendor, file_id))
    pipeline.run(sid, version, str(root / "uploads"))
    item = db.get_version(sid)
    assert item["status"] == "approved" and item["stage"] == "complete"
    assert item["vendor"]["pan"] == vendor["pan"]
    assert item["extracted"]["match"]["vendor_id"] == vendor["vendor_id"]
    assert "legal_name" not in item["metadata"]
    assert db.list_submissions()[0]["legal_name"] == vendor["legal_name"]
    assert db.list_submissions(search=vendor["vendor_id"])[0]["id"] == sid
    pipeline.run(sid, version, str(root / "uploads"))
    assert read.call_count == 1


def test_adding_evidence_does_not_hide_old_conflict_and_explicit_replacement_clears_it(runtime, monkeypatch):
    root, vendor, _ = runtime
    sid, version = upload.save_documents([attachment()])
    old_id = db.get_version(sid)["files"][0]["id"]
    def extracted(file_id):
        values = {**vendor, "bank_account": "999999999999"} if file_id == old_id else vendor
        return vendor_documents(values, file_id)
    mock_processing(monkeypatch, extracted)
    pipeline.run(sid, version, str(root / "uploads"))
    _, second = upload.save_documents([attachment()], sid)
    pipeline.run(sid, second, str(root / "uploads"))
    current = db.get_version(sid)
    assert current["status"] == "pending"
    assert any(check["code"] == "conflict_bank_account" for check in current["checks"])
    _, third = upload.save_documents([], sid, replaced_file_ids=[old_id])
    pipeline.run(sid, third, str(root / "uploads"))
    current = db.get_version(sid)
    assert current["status"] == "approved"
    assert any(not doc["active"] and doc["source"]["file_id"] == old_id for doc in current["extracted"]["documents"])
    assert db.get_version(sid, 1)["status"] == "pending"


def test_read_failure_cannot_be_ignored_when_other_evidence_is_complete(runtime, monkeypatch):
    root, vendor, _ = runtime
    sid, version = upload.save_documents([attachment(), attachment()])
    read = mock_processing(monkeypatch, lambda file_id: vendor_documents(vendor, file_id))
    bad_id = db.get_version(sid)["files"][0]["id"]
    normal_read = read.side_effect
    def read_or_fail(path, file_id, mime):
        if file_id == bad_id:
            raise ValueError("Unreadable evidence")
        return normal_read(path, file_id, mime)
    read.side_effect = read_or_fail
    pipeline.run(sid, version, str(root / "uploads"))
    item = db.get_version(sid)
    assert item["stage"] == "complete" and item["status"] == "pending"
    assert any(check["code"] == "extraction_error" for check in item["checks"])


def test_legacy_history_opens_and_old_followup_cannot_be_edited_after_new_version(runtime):
    version = db.create_version("history", {"legal_name": "Historical form name"}, [])
    db.finish("history", version, "pending", {}, [], "Old follow-up")
    legacy = db.get_version("history")
    assert legacy["workflow"] == "legacy_form" and legacy["vendor"]["legal_name"] == "Historical form name"
    upload.save_documents([attachment()], "history")
    with pytest.raises(ValueError, match="latest"):
        db.update_followup("history", version, "Changed old message")


def test_followup_can_be_read_and_edited_without_contact_email(runtime):
    sid, version = ready_for_followup({"legal_name": "Vendor without a contact email"})
    assert db.get_version(sid)["followup"] == "Please provide the missing proof."
    db.update_followup(sid, version, "Please upload readable bank proof showing the IFSC.")
    item = db.get_version(sid)
    assert item["followup"] == "Please upload readable bank proof showing the IFSC."
    assert item["events"][-1]["stage"] == "draft_edited"
    assert item["status"] == "pending"


def test_new_contact_fields_require_source_quotes(monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = model_result({"documents": [{
        "document_type": "declaration", "confidence": .99, "fields": [
            {"key": "email", "value": "vendor@example.test", "quote": "Email: vendor@example.test", "confidence": .99},
            {"key": "gst_non_registration_reason", "value": "Not registered for GST", "quote": "Not registered for GST", "confidence": .99}]}]})
    monkeypatch.setattr(llm, "_client", lambda: client)
    result = llm.extract_block({"file_id": "contact", "page": 1, "method": "mock_text",
                               "text": "Email: vendor@example.test\nNot registered for GST"}, {})
    assert {field["key"] for field in result[0]["fields"]} == {"email", "gst_non_registration_reason"}
    assert result[0]["validation_errors"] == []


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_followup_fallback_explains_issues_and_actions_when_model_fails(monkeypatch, status):
    def unavailable():
        raise RuntimeError("Mock model unavailable")
    monkeypatch.setattr(llm, "_client", unavailable)
    issue = {"code": "missing_bank_proof", "result": "pending", "message": "Bank proof is missing.",
             "action": "Upload a readable bank statement or cancelled cheque."}
    body = llm.draft_followup(status, [issue, issue], "Demo")
    assert f"Status: {status.capitalize()}" in body
    assert body.count("Bank proof is missing.") == 1
    assert "Upload a readable bank statement or cancelled cheque." in body


def test_approved_submission_needs_no_followup_or_model_call(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(llm, "_client", client)
    assert llm.draft_followup("approved", [], "Demo") == ""
    client.assert_not_called()


def test_followup_uses_message_prompt_and_returns_model_text(monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content="Pending: supply the missing bank proof."))])
    monkeypatch.setattr(llm, "_client", lambda: client)
    body = llm.draft_followup("pending", [{"code": "missing_bank_proof", "result": "pending",
                                          "message": "Bank proof is missing.", "action": "Upload bank proof."}], "Demo")
    assert body == "Pending: supply the missing bank proof."
    prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "follow-up message" in prompt and "missing or inconsistent" in prompt


def test_truncated_followup_falls_back_to_complete_issue_explanation(monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="length", message=SimpleNamespace(content="Incomplete response"))])
    monkeypatch.setattr(llm, "_client", lambda: client)
    body = llm.draft_followup("pending", [{"code": "changed_bank", "result": "pending",
        "message": "Bank details differ from the master.", "action": "Request separate bank-change verification."}], "Demo")
    assert "Incomplete response" not in body
    assert "Bank details differ from the master." in body
    assert "Request separate bank-change verification." in body


@pytest.mark.parametrize("status,stage", [("approved", "complete"), ("pending", "queued"), ("pending", "failed")])
def test_only_completed_nonapproved_message_can_be_edited(runtime, status, stage):
    sid, version = upload.save_documents([attachment()])
    db.finish(sid, version, status, {}, [], "Stored message")
    if stage != "complete":
        with db.connect() as connection:
            connection.execute("UPDATE versions SET stage=? WHERE submission_id=? AND version=?", (stage, sid, version))
    with pytest.raises(ValueError, match="completed pending or rejected"):
        db.update_followup(sid, version, "Edited message")


def test_old_delivery_events_remain_in_history_without_blocking_message_edits(runtime):
    sid, version = ready_for_followup({})
    with db.connect() as connection:
        connection.execute("INSERT INTO events(submission_id,version,at,stage,message) VALUES (?,?,?,?,?)",
                           (sid, version, db.now(), "message_sent", "Historical event from an earlier app version"))
    db.update_followup(sid, version, "Updated on-screen follow-up")
    item = db.get_version(sid)
    assert item["followup"] == "Updated on-screen follow-up"
    assert any(event["stage"] == "message_sent" for event in item["events"])
