import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import pii


def types_found(args):
    return {f["type"] for f in pii.scan(args)}


def test_detects_email():
    assert "email" in types_found({"body": "reach me at amit@example.com please"})


def test_detects_valid_card_number():
    # 4242 4242 4242 4242 is a well-known Luhn-valid test card number
    assert "card_number" in types_found({"body": "card ending 4242 4242 4242 4242"})


def test_does_not_flag_invalid_card_like_number():
    # 16 digits but fails Luhn check -> should NOT be flagged as a card
    found = types_found({"body": "order number 1234 5678 9012 3456"})
    assert "card_number" not in found


def test_detects_phone_number():
    assert "phone" in types_found({"body": "call me on +91 98765 43210"})


def test_detects_api_secret():
    found = types_found({"args": "key=sk-aaaaaaaaaaaaaaaaaaaaaaaa"})
    assert "api_secret" in found


def test_clean_text_has_no_findings():
    assert pii.scan({"table": "support_tickets", "filter": "status = 'closed'"}) == []


def test_masking_does_not_reveal_full_value():
    findings = pii.scan({"body": "amit.kulkarni@example.com"})
    email_finding = next(f for f in findings if f["type"] == "email")
    assert "amit.kulkarni@example.com" not in email_finding["value_masked"]
    assert "*" in email_finding["value_masked"]


def test_detects_ssn_shaped_number():
    assert "ssn_like" in types_found({"body": "SSN on file: 123-45-6789"})


def test_short_value_is_fully_masked_not_partially():
    # _mask() keeps `keep` chars from each end -- for a value too short
    # for that to leave anything hidden, it must fall back to masking
    # the whole thing rather than showing most of a short secret.
    assert pii._mask("ab") == "**"
    assert pii._mask("abcd", keep=2) == "****"  # len == keep*2 -- still the short-value branch
