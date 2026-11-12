"""Document profiles guide review without acting as proof of authenticity."""
from app.profiles import assess_profile, load_profiles


def test_bank_profile_recognizes_heading_or_labels():
    profiles = load_profiles()
    assert assess_profile("bank_proof", "Cancelled Cheque\nAccount Holder: A\nIFSC: HDFC0001234", profiles)["matched"]
    assert assess_profile("bank_proof", "Account Holder: A\nAccount Number: 123456789\nIFSC: HDFC0001234", profiles)["matched"]


def test_invoice_is_not_bank_profile():
    profiles = load_profiles()
    assert not assess_profile("bank_proof", "Purchase Invoice\nAmount: INR 12500", profiles)["matched"]


def test_partial_words_do_not_match_pan_labels():
    profiles = load_profiles()
    assert not assess_profile("pan", "company username", profiles)["matched"]


def test_punctuation_and_line_breaks_in_headings_are_accepted():
    profiles = load_profiles()
    assert assess_profile("pan", "PAN\nCard\nName", profiles)["matched"]
    assert assess_profile("bank_proof", "Account-Holder: Demo\nIFSC: HDFC0001234", profiles)["matched"]


def test_missing_columns_are_reported(tmp_path):
    import pytest
    path = tmp_path / "profiles.csv"
    path.write_text("wrong,columns\n", encoding="utf-8")
    with pytest.raises(ValueError, match="required columns"):
        load_profiles(path)
