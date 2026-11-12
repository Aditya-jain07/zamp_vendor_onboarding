"""Document-only rules and fixture expectations using controlled evidence.

No model calls, document generation or manually entered vendor fields are used.
"""
import copy
import json
from pathlib import Path

import pytest
from app.db import all_vendors
from app.profiles import load_profiles
from app import rules

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "data/requirements.json").read_text(encoding="utf-8"))
PROFILES = load_profiles()
VENDORS = all_vendors()


def document(kind, values, file_id='file'):
    return {'document_type': kind, 'confidence': .99, 'profile': {'matched': True},
            'source': {'file_id': file_id, 'page': 1, 'method': 'pdf_text'},
            'fields': [{'key': key, 'value': value, 'quote': value, 'confidence': .99}
                       for key, value in values.items()]}


def evidence(vendor=None):
    vendor = VENDORS[0] if vendor is None else vendor
    result = [document('pan', {k: vendor[k] for k in ('legal_name', 'pan')}),
              document('bank_proof', {k: vendor[k] for k in ('account_holder', 'bank_account', 'ifsc')})]
    if vendor['gstin']:
        result.append(document('gst', {k: vendor[k] for k in ('legal_name', 'gstin', 'address')}))
    if vendor['vendor_type'] == 'company':
        result.append(document('business_registration', {'legal_name': vendor['legal_name'], 'address': vendor['address'], 'registration_number': 'DEMO-REG'}))
    return result


def decision(docs, metadata=None, policy=None):
    return rules.evaluate(metadata or {}, docs, VENDORS, policy or POLICY, profiles=PROFILES)


def codes(checks):
    return {check['code'] for check in checks if check['result'] != 'pass'}


def test_existing_auto_matched_without_any_form():
    status, checks = decision(evidence())
    assert status == 'approved' and not codes(checks)
    assert rules.summarize_documents(evidence(), VENDORS)['match']['vendor_id'] == 'V100'


def test_legacy_form_fields_cannot_override_missing_evidence():
    status, checks = decision([], {**VENDORS[0], 'existing_vendor_id': 'V100', 'declaration_correct': True})
    assert status == 'pending'
    assert 'missing_field_pan' in codes(checks)
    assert not any(check['code'].startswith('form_') for check in checks)


@pytest.mark.parametrize('key', ['bank_account', 'ifsc', 'account_holder', 'legal_name', 'address'])
def test_changes_to_master_are_held(key):
    changed = dict(VENDORS[0])
    changed[key] = {'bank_account': '999999999999', 'ifsc': 'SBIN0004321'}.get(key, 'Changed value')
    status, checks = decision(evidence(changed))
    assert status == 'pending'
    assert ('changed_bank' if key in {'bank_account', 'ifsc', 'account_holder'} else 'existing_' + key) in codes(checks)


def test_new_vendor_stays_pending():
    vendor = {**VENDORS[0], 'pan': 'AAACX4321A', 'gstin': '27AAACX4321A1Z5'}
    status, checks = decision(evidence(vendor))
    assert status == 'pending' and 'new_vendor_verification' in codes(checks)


def test_blocked_pan_is_rejected_without_manual_id():
    status, checks = decision(evidence(VENDORS[2]))
    assert status == 'rejected' and 'blocked_record' in codes(checks)


def test_missing_bank_is_pending():
    status, checks = decision([doc for doc in evidence() if doc['document_type'] != 'bank_proof'])
    assert status == 'pending' and 'missing_bank_proof' in codes(checks)


def test_conflicting_identifiers_are_pending():
    docs = evidence() + [document('pan', {'pan': 'AAACX4321A', 'legal_name': VENDORS[0]['legal_name']})]
    status, checks = decision(docs)
    assert status == 'pending' and 'conflict_pan' in codes(checks)
    summary = rules.summarize_documents(docs, VENDORS)
    assert 'pan' not in summary['vendor'] and len(summary['field_options']['pan']) == 2


def test_multiple_master_matches_are_ambiguous():
    docs = evidence() + [document('gst', {'gstin': VENDORS[3]['gstin'], 'legal_name': VENDORS[3]['legal_name']})]
    status, checks = decision(docs)
    assert status == 'pending' and 'ambiguous_vendor' in codes(checks)


def test_gst_fallback_does_not_fill_missing_pan():
    docs = [doc for doc in evidence() if doc['document_type'] != 'pan']
    summary = rules.summarize_documents(docs, VENDORS)
    assert summary['match']['vendor_id'] == 'V100' and 'pan' not in summary['vendor']
    status, checks = decision(docs)
    assert status == 'pending' and 'missing_pan' in codes(checks)


def test_master_gst_requires_certificate_even_when_absent_from_upload():
    status, checks = decision([doc for doc in evidence() if doc['document_type'] != 'gst'])
    assert status == 'pending' and 'missing_gst' in codes(checks)


def test_existing_proprietor_without_gst_passes():
    assert decision(evidence(VENDORS[1]))[0] == 'approved'


def test_new_unregistered_vendor_needs_statement():
    vendor = {**VENDORS[1], 'pan': 'AAAPX4321A'}
    status, checks = decision(evidence(vendor))
    assert status == 'pending' and 'gst_reason' in codes(checks)


@pytest.mark.parametrize('confidence', [.2, float('nan'), True, 2])
def test_unreliable_identity_is_not_used_for_matching(confidence):
    docs = evidence()
    for doc in docs:
        doc['confidence'] = confidence
    assert rules.summarize_documents(docs, VENDORS)['match']['state'] == 'not_found'
    assert decision(docs)[0] == 'pending'


def test_required_extracted_fields_are_supported_without_form():
    docs = evidence(VENDORS[1])
    status, checks = decision(docs, policy={**POLICY, 'required_extracted_fields': ['address']})
    assert status == 'pending' and 'missing_field_address' in codes(checks)


def test_company_suffix_normalization():
    docs = evidence()
    docs[0]['fields'][0]['value'] = 'Northstar Office Supplies Pvt. Ltd.'
    assert decision(docs)[0] == 'approved'


CASES = json.loads((ROOT / "data/fixtures/cases.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((ROOT / "data/fixtures/expected_results.json").read_text(encoding="utf-8"))


def fixture_documents(case, specs, changes=None, version=1):
    """Model the visible template fields, not just the profile's mandatory subset.

    Fixture metadata is only a source for controlled evidence. It is never passed
    to evaluate() as a form, and generator functions are never imported or called.
    """
    visible_keys = {
        "pan": ["legal_name", "pan"],
        "gst": ["legal_name", "gstin", "address"],
        "bank_proof": ["account_holder", "bank_account", "ifsc"],
        "business_registration": ["legal_name", "registration_number", "address"],
        "other": [],
    }
    result = []
    for index, spec in enumerate(specs, 1):
        values = {**case, **(changes or {}), **spec.get("overrides", {})}
        values.setdefault("registration_number", f"U74999MH2024PTC{int(case['id'][1:]):06d}")
        fields = {key: values[key] for key in visible_keys[spec["kind"]] if values.get(key)}
        if spec.get("unreadable"):
            fields.pop("bank_account", None)
        doc = document(spec["kind"], fields, f"{case['id']}_v{version}_{index}")
        doc["source"]["version"] = version
        result.append(doc)
    return result


def test_answer_key_covers_every_case_once():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(EXPECTED)
    assert all(not case.get("existing_vendor_id") for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_all_fixture_initial_decisions_and_automatic_matches(case):
    docs = fixture_documents(case, case["docs"])
    status, checks = decision(docs)
    answer = EXPECTED[case["id"]]
    assert status == answer["initial"]
    assert codes(checks) == set(answer["reasons"])
    assert rules.summarize_documents(docs, VENDORS)["match"]["vendor_id"] == answer["matched_vendor_id"]


@pytest.mark.parametrize("case", [case for case in CASES if case.get("resubmission_docs")],
                         ids=[case["id"] for case in CASES if case.get("resubmission_docs")])
def test_fixture_resubmission_decisions(case):
    docs = fixture_documents(case, case["docs"])
    docs += fixture_documents(case, case["resubmission_docs"], case.get("resubmission_form"), version=2)
    status, checks = decision(docs)
    answer = EXPECTED[case["id"]]
    assert status == answer["after_resubmission"]
    assert codes(checks) == set(answer["after_resubmission_reasons"])
    assert rules.summarize_documents(docs, VENDORS)["match"]["vendor_id"] == answer["after_resubmission_matched_vendor_id"]


def test_blank_bank_field_does_not_satisfy_completeness():
    docs = evidence()
    bank = next(doc for doc in docs if doc["document_type"] == "bank_proof")
    next(field for field in bank["fields"] if field["key"] == "bank_account")["value"] = " "
    status, checks = decision(docs)
    assert status == "pending" and "unreadable_bank_proof_bank_account" in codes(checks)


def test_separate_partial_entries_do_not_silently_form_a_complete_bank_proof():
    docs = [doc for doc in evidence() if doc["document_type"] != "bank_proof"]
    vendor = VENDORS[0]
    docs += [document("bank_proof", {"account_holder": vendor["account_holder"]}, "holder"),
             document("bank_proof", {"bank_account": vendor["bank_account"], "ifsc": vendor["ifsc"]}, "account")]
    status, checks = decision(docs)
    assert status == "pending" and "incomplete_bank_proof" in codes(checks)


def test_superseded_evidence_is_retained_but_ignored_by_rules():
    docs = evidence()
    old = document("bank_proof", {"bank_account": "999999999999"}, "old")
    old["active"] = False
    assert decision(docs + [old])[0] == "approved"


def test_pan_holder_category_conflicts_with_gst_matched_master():
    docs = evidence()
    next(field for field in docs[0]["fields"] if field["key"] == "pan")["value"] = "AAAPD1234A"
    status, checks = decision(docs)
    assert status == "pending" and "pan_holder_type" in codes(checks)


def test_document_profile_mismatch_prevents_approval():
    docs = evidence()
    docs[0]["profile"]["matched"] = False
    status, checks = decision(docs)
    assert status == "pending" and "unfamiliar_document_profile" in codes(checks)


def test_no_contact_or_manual_declaration_is_required():
    docs = evidence()
    assert not any(field["key"] in {"email", "phone", "contact_name", "declaration_correct"}
                   for doc in docs for field in doc["fields"])
    assert decision(docs)[0] == "approved"


