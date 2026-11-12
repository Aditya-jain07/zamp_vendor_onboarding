"""Explicit demo policy. No LLM response is treated as a decision."""
import re
import math

from .profiles import load_profiles

PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACCOUNT = re.compile(r"^[0-9]{9,18}$")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE = re.compile(r"^[0-9+()\s-]{10,18}$")


def clean(value: object) -> str:
    return str(value or "").strip()


def canon(value: object) -> str:
    text = clean(value).lower().replace("&", "and")
    text = re.sub(r"\b(pvt|pvt\.|private)\b", "private", text)
    text = re.sub(r"\b(ltd|ltd\.|limited)\b", "limited", text)
    return re.sub(r"[\W_]", "", text)


def _reliable(value: object, threshold: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and
            math.isfinite(value) and threshold <= value <= 1)


def normalized(key: str, value: object) -> str:
    if key in {"pan", "gstin", "ifsc", "bank_account"}:
        return re.sub(r"\s+", "", clean(value)).upper()
    if key in {"legal_name", "trading_name", "account_holder", "address"}:
        return canon(value)
    return clean(value).casefold()


def evidence_values(documents: list[dict]) -> dict[str, list[dict]]:
    """Only reliable document fields participate in identification and comparison."""
    values: dict[str, list[dict]] = {}
    for document in documents:
        if not document.get("active", True) or not _reliable(document.get("confidence"), .65):
            continue
        for field in document.get("fields", []):
            if clean(field.get("value")) and _reliable(field.get("confidence"), .6):
                values.setdefault(field["key"], []).append({
                    "value": clean(field["value"]), "source": document.get("source"),
                    "document_type": document["document_type"]})
    return values


def summarize_documents(documents: list[dict], vendors: list[dict]) -> dict:
    """Resolve identifiers without inventing fields from a master record or a form."""
    values = evidence_values(documents)
    vendor = {}
    options = {}
    for key, entries in values.items():
        unique = {}
        for entry in entries:
            unique.setdefault(normalized(key, entry["value"]), entry["value"])
        if len(unique) == 1:
            vendor[key] = next(iter(unique.values()))
            if key in {"pan", "gstin", "ifsc", "bank_account"}:
                vendor[key] = normalized(key, vendor[key])
        else:
            options[key] = list(unique.values())

    pans = {normalized("pan", entry["value"]) for entry in values.get("pan", [])
            if PAN.fullmatch(normalized("pan", entry["value"]))}
    gsts = {normalized("gstin", entry["value"]) for entry in values.get("gstin", [])
            if GSTIN.fullmatch(normalized("gstin", entry["value"]))}
    matches = [row for row in vendors if normalized("pan", row.get("pan")) in pans or
               (row.get("gstin") and normalized("gstin", row["gstin"]) in gsts)]
    match_state = "matched" if len(matches) == 1 else "ambiguous" if matches else "not_found"
    current = matches[0] if len(matches) == 1 else None
    pan = vendor.get("pan", "")
    if current:
        vendor_type, basis = current["vendor_type"], "vendor_master"
    elif PAN.fullmatch(pan):
        vendor_type = "company" if pan[3] == "C" else "individual" if pan[3] == "P" else "unknown"
        basis = "pan_holder_category"
    elif any(doc.get("active", True) and doc["document_type"] == "business_registration" for doc in documents):
        vendor_type, basis = "company", "business_registration_document"
    else:
        vendor_type, basis = "unknown", "unresolved"
    # Type is routing metadata; it is explicitly distinguished from extracted fields.
    return {"vendor": vendor, "field_options": options, "vendor_type": vendor_type,
            "vendor_type_basis": basis, "match": {"state": match_state,
            "vendor_id": current["vendor_id"] if current else None,
            "candidate_vendor_ids": [row["vendor_id"] for row in matches],
            "master_snapshot": dict(current) if current else None}}


def evaluate(metadata: dict, documents: list[dict], vendors: list[dict], policy: dict,
             extraction_errors: list[str] | None = None,
             profiles: dict[str, dict] | None = None) -> tuple[str, list[dict]]:
    """Document-only decision. Legacy form values in metadata are never evidence.

    The first argument is retained so existing callers import cleanly. New uploads
    store only workflow/replacement metadata in it. required_form_fields in an old
    requirements.json is deliberately ignored by this workflow.
    """
    profiles = load_profiles() if profiles is None else profiles
    summary = summarize_documents(documents, vendors)
    values = evidence_values(documents)
    vendor = summary["vendor"]
    current = summary["match"]["master_snapshot"]
    checks: list[dict] = []

    def add(code, result, message, action="", sources=None):
        checks.append({"code": code, "result": result, "message": message,
                       "action": action, "sources": sources or []})

    def sources(key):
        return [entry["source"] for entry in values.get(key, [])]

    active = [doc for doc in documents if doc.get("active", True)]
    by_type = {}
    for doc in active:
        kind = doc["document_type"]
        by_type.setdefault(kind, []).append(doc)
        if not _reliable(doc.get("confidence"), .65):
            add("uncertain_classification", "pending", "A document type is uncertain.",
                "Provide clearer evidence or request manual review.", [doc.get("source")])
        if doc.get("validation_errors"):
            add("unsupported_extraction", "pending", "Some extracted fields lack reliable source quotes.",
                "Review the source or retry extraction.", [doc.get("source")])
        if kind in profiles and doc.get("profile") is not None and not doc["profile"]["matched"]:
            add("unfamiliar_document_profile", "pending", f"The {kind.replace('_', ' ')} has unfamiliar headings or labels.",
                "Review its type manually; profile matching does not authenticate it.", [doc.get("source")])
        for field in doc.get("fields", []):
            if clean(field.get("value")) and not _reliable(field.get("confidence"), .6):
                add("uncertain_field", "pending", f"Extracted {field['key'].replace('_', ' ')} is uncertain.",
                    "Provide clearer evidence.", [doc.get("source")])

    required = policy.get("required_documents", {})
    if not isinstance(required, dict):
        raise ValueError("required_documents must be an object")
    vendor_type = summary["vendor_type"]
    if vendor_type == "unknown":
        add("vendor_type", "pending", "Vendor type could not be established from the evidence or vendor master.",
            "Request identity or business-registration evidence.")
    kinds = required.get(vendor_type, ["pan", "bank_proof"])
    if not isinstance(kinds, list) or any(kind not in profiles for kind in kinds):
        raise ValueError("required_documents contains an unsupported document type")
    # Core identity/bank proof must not disappear because an older config omits a type.
    kinds = list(dict.fromkeys(["pan", "bank_proof"] + kinds +
                              (["business_registration"] if vendor_type == "company" else [])))
    needs_gst = bool(values.get("gstin") or by_type.get("gst") or (current and current.get("gstin")))
    if needs_gst:
        kinds.append("gst")
    for kind in dict.fromkeys(kinds):
        docs = by_type.get(kind, [])
        if not docs:
            add(f"missing_{kind}", "pending", f"Required {kind.replace('_', ' ')} is missing.", "Upload the correct document.")
            continue
        required_fields = profiles.get(kind, {}).get("required_fields", [])
        if not required_fields:
            if kind in {"pan", "gst", "bank_proof", "business_registration"}:
                raise ValueError(f"Configure required fields for {kind}")
            continue
        sets = [{field["key"] for field in doc.get("fields", [])
                 if clean(field.get("value")) and _reliable(field.get("confidence"), .6)}
                if _reliable(doc.get("confidence"), .65) else set() for doc in docs]
        found = set().union(*sets)
        for key in required_fields:
            if key not in found:
                add(f"unreadable_{kind}_{key}", "pending",
                    f"{kind.replace('_', ' ')} does not clearly show {key.replace('_', ' ')}.",
                    "Provide readable, complete evidence.", [doc.get("source") for doc in docs])
        if set(required_fields) <= found:
            if any(set(required_fields) <= fields for fields in sets):
                add(f"document_{kind}", "pass", f"{kind.replace('_', ' ').capitalize()} is present and readable.",
                    sources=[doc.get("source") for doc in docs])
            else:
                add(f"incomplete_{kind}", "pending", f"Required {kind.replace('_', ' ')} fields are spread across separate entries.",
                    "Request manual review of the document/page grouping.", [doc.get("source") for doc in docs])

    core = policy.get("required_extracted_fields", ["legal_name", "pan", "account_holder", "bank_account", "ifsc"])
    allowed = {"legal_name", "trading_name", "pan", "gstin", "account_holder", "bank_account", "ifsc",
               "registration_number", "address", "contact_name", "email", "phone", "gst_non_registration_reason"}
    if not isinstance(core, list) or any(key not in allowed for key in core):
        raise ValueError("required_extracted_fields contains unsupported fields")
    for key in set(core) | {"legal_name", "pan", "account_holder", "bank_account", "ifsc"}:
        if not values.get(key):
            add(f"missing_field_{key}", "pending", f"Documents do not provide a readable {key.replace('_', ' ')}.",
                "Request the missing information as supporting evidence.")
    for key in ("pan", "gstin", "bank_account", "ifsc", "legal_name", "account_holder", "address"):
        if key in summary["field_options"]:
            add(f"conflict_{key}", "pending", f"Documents contain conflicting {key.replace('_', ' ')} values.",
                "Confirm the correct value with consistent evidence.", sources(key))
    for key, pattern, code in (("pan", PAN, "pan_format"), ("gstin", GSTIN, "gst_format"),
                               ("ifsc", IFSC, "ifsc_format"), ("bank_account", ACCOUNT, "account_format")):
        entries = values.get(key, [])
        if entries:
            valid = all(pattern.fullmatch(normalized(key, entry["value"])) for entry in entries)
            add(code, "pass" if valid else "pending", f"Document {key.replace('_', ' ')} format " + ("checks passed." if valid else "is invalid."),
                "" if valid else "Request corrected, readable evidence.", sources(key))
    pan = vendor.get("pan", "")
    if pan and PAN.fullmatch(pan):
        if vendor_type == "company" and pan[3] != "C" or vendor_type in {"sole_proprietor", "individual"} and pan[3] != "P":
            add("pan_holder_type", "pending", "PAN holder category differs from the vendor type.", "Request identity review.", sources("pan"))
        if any(GSTIN.fullmatch(normalized("gstin", entry["value"])) and normalized("gstin", entry["value"])[2:12] != pan
               for entry in values.get("gstin", [])):
            add("gst_pan", "pending", "GST evidence embeds a different PAN from the PAN evidence.",
                "Request consistent tax documents.", sources("pan") + sources("gstin"))
    if not needs_gst and not current and not vendor.get("gst_non_registration_reason"):
        add("gst_reason", "pending", "GST registration evidence or an explicit non-registration statement is missing.",
            "Request a GST certificate or a written non-registration explanation.")

    legal = vendor.get("legal_name")
    holder = vendor.get("account_holder")
    established_holder = current and normalized("account_holder", holder) == normalized("account_holder", current.get("account_holder"))
    if legal and holder and canon(legal) != canon(holder) and not established_holder:
        add("bank_name", "pending", "The bank account-holder name differs from the legal name.",
            "Request evidence linking the account holder to the vendor.", sources("account_holder") + sources("legal_name"))
    if summary["match"]["state"] == "ambiguous":
        add("ambiguous_vendor", "pending", "Document identifiers point to multiple vendor-master records.", "Request internal identity review.")
    elif current:
        add("vendor_match", "pass", "Document identifiers match vendor " + current["vendor_id"] + ".")
        for key in ("pan", "gstin", "legal_name", "address", "bank_account", "ifsc", "account_holder"):
            entries = values.get(key, [])
            if entries and any(normalized(key, entry["value"]) != normalized(key, current.get(key)) for entry in entries):
                code = "changed_bank" if key in {"bank_account", "ifsc", "account_holder"} else f"existing_{key}"
                if not any(check["code"] == code for check in checks):
                    add(code, "pending", f"Document {key.replace('_', ' ')} differs from the approved vendor master.",
                        "Request separate verification before accepting the change.", sources(key))
        if current.get("status") not in {"approved", "blocked"}:
            add("existing_status", "pending", "The matched vendor record is not approved.", "Request internal review.")
    else:
        add("new_vendor_verification", "pending", "No vendor-master record matches the document identifiers.",
            "Procurement must verify this vendor before approval.")

    # Any reliable document identifier linked to a blocked record invokes the policy.
    blocked = [row for row in vendors if row.get("status") == "blocked" and any(
        normalized("pan", entry["value"]) == normalized("pan", row.get("pan")) for entry in values.get("pan", []))]
    if blocked or current and current.get("status") == "blocked":
        add("blocked_record", "reject", "Document identifiers match an internally blocked vendor record.", "Contact procurement for internal review.")
    for error in extraction_errors or []:
        add("extraction_error", "pending", f"A file could not be read fully. {error}", "Provide a readable replacement or request manual review.")
    status = "rejected" if any(check["result"] == "reject" for check in checks) else "pending" if any(
        check["result"] == "pending" for check in checks) else "approved"
    if status == "approved":
        add("complete", "pass", "Document checks passed against an existing approved vendor record.")
    return status, checks


