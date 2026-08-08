"""Tests for normalizer - amount direction, UTR validation, fingerprinting."""

import hashlib
from datetime import datetime

from app.normalizer import (
    exact_key,
    extract_utr,
    fingerprint_key,
    merchant_from_text,
    normalize_counterparty,
    parse_amount,
)


def test_parse_amount_currency_prefix():
    amount, currency = parse_amount("Rs.900.00 debited from A/c **1234", direction_hint="debit")
    assert amount == -90000
    assert currency == "INR"


def test_parse_amount_bare_with_debit_word():
    # Prototype Bug D: bare amount must get direction from words
    amount, _ = parse_amount("450.00 debited from A/c", direction_hint=None)
    assert amount == -45000


def test_parse_amount_bare_with_credit_word():
    amount, _ = parse_amount("600.00 credited to A/c", direction_hint=None)
    assert amount == 60000


def test_parse_amount_bare_no_direction_defaults_positive():
    # Ambiguous bare amount: default positive, caller flags needs_review
    amount, _ = parse_amount("450.00 towards purchase", direction_hint=None)
    assert amount == 45000


def test_extract_utr_requires_label():
    # Prototype Bug E: bare 12-digit number is NOT a UTR
    assert extract_utr("order 412345678999 placed") is None
    assert extract_utr("Ref: 412345678999") == "412345678999"
    assert extract_utr("Ref No 412345678999") == "412345678999"
    assert extract_utr("UPI: 412345678999") == "412345678999"


def test_normalize_counterparty_strips_vpa():
    assert normalize_counterparty("ravi@ybl") == "ravi"
    assert normalize_counterparty("UPI: Swiggy") == "swiggy"
    assert normalize_counterparty("  PAYTM  ") == "paytm"


def test_merchant_from_text():
    assert merchant_from_text("paid by UPI: swiggy on 03-08-26") == "swiggy"
    assert merchant_from_text("debited at zomato") == "zomato"


def test_fingerprint_key_is_signed():
    # Bug C: signed amount in the key - credit and debit must differ
    d = datetime(2026, 8, 3)
    credit = fingerprint_key(60000, d, "ravi", "UPI", "1234")
    debit = fingerprint_key(-60000, d, "ravi", "UPI", "1234")
    assert credit != debit


def test_exact_key_includes_utr():
    d = datetime(2026, 8, 3)
    k1 = exact_key(-90000, d, "swiggy", "UPI", "1234", "412345678999")
    k2 = exact_key(-90000, d, "swiggy", "UPI", "1234", "999999999999")
    assert k1 != k2
    assert len(k1) == 64  # sha256
