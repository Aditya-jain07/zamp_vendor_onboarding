"""Vendor onboarding workspace. Run with `streamlit run app/streamlit_app.py`."""
import os
import sys
import uuid
from pathlib import Path
from zipfile import ZipFile

import pymupdf as fitz
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app import db  # noqa: E402
from app.documents import docx_text_blocks  # noqa: E402
from app.pipeline import run  # noqa: E402
from app.rules import GSTIN, PAN, evidence_values, normalized  # noqa: E402
from app.upload import save_documents, upload_root  # noqa: E402

st.set_page_config(page_title="Vendor onboarding", page_icon=":material/fact_check:",
                   layout="wide", initial_sidebar_state="expanded")
db.init()


def navigate(page: str, submission_id: str | None = None) -> None:
    st.session_state.page = page
    if submission_id is not None:
        st.session_state.selected_submission = submission_id
    if page == "New submission" and submission_id is None:
        st.session_state.resubmit_id = None
        st.session_state.new_form_id = uuid.uuid4().hex
        st.session_state.pop("selected_submission", None)
        st.session_state.pop("requested_attempt", None)
    st.rerun()


def mask(value: object) -> str:
    text = str(value or "")
    return "•••• " + text[-4:] if text else "—"


def status_box(status: str, message: str) -> None:
    if status == "approved":
        st.success(message)
    elif status == "rejected":
        st.error(message)
    elif status == "pending":
        st.warning(message)
    else:
        st.info(message)


def vendor_match_explanation(item: dict) -> tuple[str, str]:
    """Keep 'identifier unavailable' distinct from a completed search with no match."""
    extracted = item.get("extracted") or {}
    match = extracted.get("match") or {}
    if match.get("vendor_id"):
        return match["vendor_id"], "Document identifiers matched this registered vendor during the review."
    if match.get("state") == "ambiguous":
        return "Multiple possible records", "Document identifiers point to more than one vendor record. Identity needs manual review."
    if match.get("state") != "not_found":
        return "Matching result unavailable", "This review has no saved automatic matching result."
    options = extracted.get("field_options") or {}
    if options.get("pan") or options.get("gstin"):
        return "Identity matching needs review", "The documents contain conflicting PAN/GSTIN values. Resolve those conflicts before concluding whether this is a new vendor."
    identity = item.get("vendor") or {}
    has_identifier = bool(PAN.fullmatch(normalized("pan", identity.get("pan"))) or
                          GSTIN.fullmatch(normalized("gstin", identity.get("gstin"))))
    if not has_identifier:
        return "Not enough valid identity information to match", "A unique, readable PAN or GSTIN is needed. Missing or invalid identifiers do not establish that the vendor is absent from the database."
    return "No matching registered vendor", "No vendor record matched the extracted PAN/GSTIN during this review. This is separate from the missing-document issues shown above."


def sidebar() -> str:
    st.sidebar.title("Vendor onboarding")
    if st.sidebar.button("New review", icon=":material/add:", use_container_width=True):
        navigate("New submission")
    st.sidebar.divider()
    st.sidebar.subheader("History")
    query = st.sidebar.text_input("Search reviews", placeholder="Search company or submission ID")
    with st.sidebar.expander("Filter history"):
        selected_status = st.selectbox("Status", ["All", "Approved", "Pending", "Rejected", "Processing"],
                                       key="history_status")
    items = db.list_review_history(query, "" if selected_status == "All" else selected_status.lower())
    selected_id = st.session_state.get("selected_submission")
    selected_attempt = None
    if selected_id and st.session_state.get("page") == "Submission detail":
        latest = db.get_version(selected_id)
        if latest:
            selected_attempt = st.session_state.get(
                "requested_attempt",
                st.session_state.get(f"attempt_{selected_id}_{latest['version']}", latest["version"]),
            )
    icons = {"approved": ":material/check_circle:", "pending": ":material/schedule:",
             "rejected": ":material/cancel:", "processing": ":material/pending:"}
    if not items:
        st.sidebar.caption("No matching reviews." if query or selected_status != "All" else "No reviews yet.")
    for item in items:
        active = selected_id == item["id"] and selected_attempt == item["version"]
        with st.sidebar.container():
            if st.button(f"{item['legal_name']} ({item['version']})",
                         key=f"history_{item['id']}_{item['version']}",
                         icon=icons.get(item["status"], ":material/description:"),
                         type="secondary" if active else "tertiary", use_container_width=True,
                         help=item["flag"] or f"Submission {item['id']}"):
                st.session_state.resubmit_id = None
                st.session_state.requested_attempt = item["version"]
                navigate("Submission detail", item["id"])
            status = item["status"].capitalize()
            if item["stage"] == "failed":
                status += " · Processing failed"
            st.caption(status)
    # Keep a stale page selection from the earlier dashboard on the upload home.
    page = st.session_state.get("page", "New submission")
    return page if page in {"New submission", "Submission detail"} else "New submission"


def document_upload(prefix: str, previous: dict | None = None) -> tuple[bool, list]:
    with st.form(f"document_upload_{prefix}"):
        st.subheader("Upload vendor documents")
        st.caption("Upload the documents received from the vendor. The system extracts their details and checks them against your vendor master.")
        st.caption("Supported files: PDF, PNG, JPEG and DOCX. A combined PDF or several files count as one submission.")
        if previous:
            st.caption("Add missing documents or select earlier files to replace. Leave uploads empty to reprocess retained documents.")
        uploads = st.file_uploader("Supporting files", type=["pdf", "png", "jpg", "jpeg", "docx"],
                                   accept_multiple_files=True, key=f"{prefix}_files")
        submitted = st.form_submit_button("Check documents", type="primary", use_container_width=True)
    return submitted, uploads or []


def new_submission() -> None:
    resubmit_id = st.session_state.get("resubmit_id")
    previous = db.get_version(resubmit_id) if resubmit_id else None
    if previous:
        st.title("Review additional or corrected documents")
        st.caption(f"This creates review attempt {previous['version'] + 1} for the same submission. Earlier files and decisions remain in its history.")
    else:
        st.title("Vendor onboarding")
    replaced = []
    if previous:
        excluded = set(previous["form"].get("_superseded_file_ids") or [])
        files = {file["id"]: file for file in previous["files"] if file["id"] not in excluded}
        replaced = st.multiselect(
            "Earlier files replaced by this upload",
            list(files),
            format_func=lambda file_id: f"v{files[file_id]['version']} · {files[file_id]['original_name']} · {file_id[:8]}",
            key=f"replace_{resubmit_id}_{previous['version'] + 1}",
            help="Choose files that contain incorrect or unreadable evidence. Their originals remain in the history. "
                 "A combined file must be replaced with all the evidence it contained. Leave empty to add missing evidence.",
        )
    prefix = (f"{resubmit_id}_{previous['version'] + 1}" if previous else
              "new_" + st.session_state.setdefault("new_form_id", uuid.uuid4().hex))
    if not os.getenv("GROQ_API_KEY"):
        st.warning("Configure GROQ_API_KEY in .env before checking documents.")
    submitted, uploads = document_upload(prefix, previous)
    if not submitted:
        return
    try:
        submission_id, version = save_documents(uploads, resubmit_id, replaced_file_ids=replaced,
                                                   expected_version=previous["version"] if previous else None)
    except (ValueError, OSError) as exc:
        st.error(str(exc))
        return
    st.session_state.selected_submission = submission_id
    st.session_state.resubmit_id = None
    st.session_state.requested_attempt = version
    progress = st.status("Reviewing documents", expanded=True)
    progress.write("queued · Submission accepted")
    run(submission_id, version, str(upload_root()),
        on_stage=lambda stage, message: progress.write(f"{stage} · {message}"))
    result = db.get_version(submission_id, version)
    if result and result["stage"] == "complete":
        progress.update(label=f"Review complete · {result['status']}", state="complete", expanded=True)
    else:
        progress.update(label="Processing stopped · review required", state="error", expanded=True)
    if result:
        status_box(result["status"], f"Decision: {result['status'].upper()} · submission {submission_id[:8]} · version {version}")
    navigate("Submission detail", submission_id)


def source_label(source: dict, names: dict[str, str]) -> str:
    locator = source.get("page", "?")
    place = f"page {locator}" if isinstance(locator, int) else f"section {locator}"
    return f"{names.get(source.get('file_id'), source.get('file_id', '?'))}, {place}"


def comparison_rows(item: dict) -> list[dict]:
    """Show every saved candidate; a conflict must not look like missing data."""
    extracted = item.get("extracted") or {}
    master = (extracted.get("match") or {}).get("master_snapshot") or {}
    identity = item.get("vendor") or {}
    options = extracted.get("field_options") or {}
    evidence = evidence_values(extracted.get("documents") or [])
    names = {file["id"]: file["original_name"] for file in item.get("files", [])}
    rows = []
    for key in ("legal_name", "pan", "gstin", "account_holder", "bank_account", "ifsc", "address"):
        actual, expected = identity.get(key, ""), master.get(key, "")
        candidates = options.get(key) or ([actual] if actual else [])
        conflict = bool(options.get(key))
        result = ("Conflicting evidence" if conflict else "Not extracted" if not actual else
                  "No matched record" if not master else
                  "Matches" if normalized(key, actual) == normalized(key, expected) else "Differs")
        details = []
        for value in candidates:
            shown = mask(value) if key == "bank_account" else str(value)
            sources = [source_label(entry.get("source") or {}, names)
                       for entry in evidence.get(key, [])
                       if normalized(key, entry["value"]) == normalized(key, value)]
            check = ("Matches stored record" if master and normalized(key, value) == normalized(key, expected)
                     else "Differs from stored record" if master else "No matched record")
            details.append({"value": shown, "sources": list(dict.fromkeys(sources)), "check": check})
        rows.append({"Field": key.upper() if key in {"pan", "gstin", "ifsc"} else key.replace("_", " ").title(),
                     "Documents": "\n".join(detail["value"] for detail in details) or "—",
                     "Vendor master": mask(expected) if key == "bank_account" else expected or "—",
                     "Check": result, "Candidates": details, "Conflict": conflict})
    return rows


def evidence_panel(item: dict) -> None:
    st.subheader("Extracted evidence")
    documents = (item.get("extracted") or {}).get("documents", [])
    names = {file["id"]: file["original_name"] for file in item["files"]}
    if not documents:
        st.info("No document evidence extracted yet.")
        return
    for document in documents:
        label = (f"{document['document_type'].replace('_', ' ').title()} · "
                 f"{source_label(document['source'], names)} · "
                 f"{'active' if document.get('active', True) else 'superseded'}")
        with st.expander(label):
            st.caption(f"Classification confidence: {document.get('confidence', 0):.0%} · method: {document['source'].get('method')}")
            profile = document.get("profile")
            if profile is not None:
                st.caption("Document profile: " + ("headings/labels matched" if profile["matched"] else "unfamiliar layout - review needed"))
            if not document["fields"]:
                st.warning("No reliable fields were extracted from this document.")
            for field in document["fields"]:
                value = mask(field["value"]) if field["key"] == "bank_account" else field["value"]
                st.write(f"**{field['key'].replace('_', ' ').title()}:** {value}")
                if field["key"] != "bank_account":
                    st.caption(f"Source quote: {field['quote']}")


def preview_file(item: dict) -> None:
    try:
        _preview_file(item)
    except Exception:
        st.error("This file could not be previewed. Download the original or upload a readable replacement.")


def _preview_file(item: dict) -> None:
    st.subheader("Source preview")
    files = item["files"]
    if not files:
        st.info("No files stored.")
        return
    choices = {f"v{file['version']} · {file['original_name']} · {file['id'][:8]}": file for file in files}
    selection = st.selectbox("File", list(choices), key=f"preview_{item['submission_id']}_{item['version']}")
    file = choices[selection]
    location = upload_root() / item["submission_id"] / file["stored_name"]
    if not location.is_file():
        st.error("Source file is missing from storage.")
        return
    st.download_button("Download original", location.read_bytes(), file_name=file["original_name"],
                       mime=file["mime"], key=f"download_{file['id']}")
    if file["mime"] == "application/pdf":
        with fitz.open(str(location)) as pdf:
            if not pdf.page_count:
                st.warning("This PDF contains no pages.")
                return
            page_number = st.selectbox("Page", list(range(1, pdf.page_count + 1)),
                                       key=f"page_{file['id']}")
            page = pdf[page_number - 1]
            scale = min(1.4, 1900 / max(1, page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            st.image(pix.tobytes("png"), caption=f"Page {page_number}", use_container_width=True)
    elif file["mime"].startswith("image/"):
        st.image(str(location), caption=file["original_name"], use_container_width=True)
    else:
        for label, text in docx_text_blocks(location):
            st.caption(label)
            st.text(text)
        with ZipFile(location) as archive:
            images = [name for name in archive.namelist() if name.startswith("word/media/") and not name.endswith("/")]
            if len(images) > 8:
                st.warning("Preview shows the first eight embedded images. Split this DOCX into smaller uploads for review.")
            for name in images[:8]:
                if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    st.image(archive.read(name), caption=name, use_container_width=True)


def checks_panel(item: dict) -> None:
    st.subheader("Decision checks")
    checks = item.get("checks") or []
    if not checks:
        st.info("Checks appear when processing completes.")
        return
    names = {file["id"]: file["original_name"] for file in item["files"]}
    for check in checks:
        with st.container(border=True):
            status_box("approved" if check["result"] == "pass" else
                       "rejected" if check["result"] == "reject" else "pending", check["message"])
            if check.get("action"):
                st.write("Next action:", check["action"])
            if check.get("sources"):
                st.caption("Evidence: " + "; ".join(source_label(s, names) for s in check["sources"] if s))


def followup_panel(item: dict) -> None:
    if item["status"] == "approved":
        return
    issues = [check for check in item.get("checks") or [] if check["result"] != "pass"]
    fallback = "\n".join(f"- {check['message']} {check.get('action', '')}".strip() for check in issues)
    body = item.get("followup") or fallback or item.get("error")
    if not body:
        return
    st.subheader("Follow-up message")
    latest = db.get_version(item["submission_id"])
    is_latest = bool(latest and latest["version"] == item["version"])
    editable = is_latest and item["stage"] == "complete" and item["status"] in {"pending", "rejected"}
    draft = st.text_area("Mistakes, missing information and next steps", value=body,
                         height=220, disabled=not editable,
                         key=f"followup_{item['submission_id']}_{item['version']}")
    if not is_latest:
        st.info("This message belongs to an earlier version. Open the latest version for the current follow-up.")
        return
    st.caption("Select the text to copy it. You can edit and save the message after review completes.")
    if editable and st.button("Save edited message", disabled=not draft.strip() or draft == item.get("followup")):
        try:
            db.update_followup(item["submission_id"], item["version"], draft)
            st.success("Follow-up message saved in the audit trail.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def detail() -> None:
    submission_id = st.session_state.get("selected_submission")
    if not submission_id:
        st.info("Select a review from the sidebar history.")
        return
    latest = db.get_version(submission_id)
    if not latest:
        st.error("Submission not found. Select another review from the sidebar.")
        return
    attempt_key = f"attempt_{submission_id}_{latest['version']}"
    requested = st.session_state.pop("requested_attempt", None)
    if requested in range(1, latest["version"] + 1):
        st.session_state[attempt_key] = requested
    version = st.session_state.get(attempt_key, latest["version"])
    if version not in range(1, latest["version"] + 1):
        version = latest["version"]
        st.session_state[attempt_key] = version
    item = db.get_version(submission_id, version)
    if not item:
        st.error("Submission version not found.")
        return
    st.title(f"{item['vendor'].get('legal_name') or 'Unidentified vendor'} ({version})")
    st.caption(f"Review attempt {version} · submission {submission_id} · {item['created_at']} UTC")
    status_box(item["status"], "Decision: " + item["status"].upper())
    st.caption("Processing complete" if item["stage"] == "complete" else "Processing stage: " + item["stage"])
    if item.get("error"):
        st.error(item["error"])
    missing = [check for check in item.get("checks") or []
               if check["result"] != "pass" and (check["code"] in {
                   "missing_pan", "missing_bank_proof", "missing_gst", "missing_business_registration", "gst_reason"})]
    if missing:
        st.subheader("Missing documents or information")
        for check in missing:
            st.warning(check["message"])
    followup_panel(item)
    st.divider()
    top_left, top_right = st.columns([1, 1])
    with top_left:
        st.subheader("Vendor details from documents")
        identity = item["vendor"]
        extracted = item.get("extracted") or {}
        if item["workflow"] == "legacy_form":
            st.warning("This historical run used the earlier form-based workflow. Create a new version to check the documents automatically.")
        match = extracted.get("match", {})
        match_label, match_details = vendor_match_explanation(item)
        st.write("**Registered vendor:**", match_label)
        st.caption(match_details)
        st.write("**Vendor type:**", extracted.get("vendor_type") or "Unresolved")
        if extracted.get("vendor_type_basis"):
            st.caption("Type determined from: " + extracted["vendor_type_basis"].replace("_", " "))
        for key in ("legal_name", "pan", "gstin", "account_holder", "ifsc", "address"):
            label = key.upper() if key in {"pan", "gstin", "ifsc"} else key.replace("_", " ").title()
            value = identity.get(key) or ("Multiple extracted values — see comparison below"
                                         if extracted.get("field_options", {}).get(key) else "—")
            st.write(f"**{label}:** {value}")
        st.write("**Bank account:**", mask(identity.get("bank_account")))
        for key, values in extracted.get("field_options", {}).items():
            shown = [mask(value) if key == "bank_account" else value for value in values]
            st.warning(f"Conflicting {key.replace('_', ' ')}: " + " / ".join(shown))
        with st.expander("Compare with registered vendor record"):
            master = match.get("master_snapshot") or {}
            if not master:
                st.info(match_details)
            for row in comparison_rows(item):
                with st.container(border=True):
                    st.caption(row["Field"])
                    document_column, master_column = st.columns(2)
                    document_column.text("Documents: " + str(row["Documents"]))
                    master_column.text("Stored record: " + str(row["Vendor master"]))
                    st.caption("Check: " + row["Check"])
                    if row["Conflict"]:
                        for index, candidate in enumerate(row["Candidates"], 1):
                            st.text(f"Value {index}: {candidate['value']}")
                            st.caption(candidate["check"])
                            if candidate["sources"]:
                                st.caption("Source: " + "; ".join(candidate["sources"]))
        with st.expander("Show full extracted account number for reviewer"):
            st.code(identity.get("bank_account") or "No unique readable account number extracted")
    with top_right:
        st.subheader("Run timeline")
        for event in item["events"]:
            st.write(f"**{event['stage'].title()}** · {event['message']}")
            st.caption(event["at"] + " UTC")
    st.divider()
    tab_checks, tab_evidence, tab_files = st.tabs(
        ["Decision checks", "Extracted evidence", "Source files"])
    with tab_checks:
        checks_panel(item)
    with tab_evidence:
        evidence_panel(item)
    with tab_files:
        preview_file(item)
    st.divider()
    if version == latest["version"] and st.button("Upload a correction / new version", type="primary"):
        st.session_state.resubmit_id = submission_id
        navigate("New submission", submission_id)


page = sidebar()
if page == "New submission":
    new_submission()
else:
    detail()
