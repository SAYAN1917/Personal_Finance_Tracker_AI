"""Tests for the NLU AI lane (Phase 7, Section 6.3)."""

from __future__ import annotations

from app.nlu import _validate


def test_validate_clean_json():
    out = _validate('{"amount_paise": 45000, "direction": "debit", "intent": "expense", "category": "food"}')
    assert out["amount_paise"] == 45000
    assert out["intent"] == "expense"
    assert out["category"] == "food"


def test_validate_rejects_bad_category():
    # category outside the allowlist is coerced to None (never silently wrong)
    out = _validate('{"intent": "expense", "category": "casino"}')
    assert out["category"] is None


def test_validate_rejects_non_json():
    assert _validate("dinner 450") is None
    assert _validate("") is None
    assert _validate("[1,2,3]") is None


def test_validate_strips_code_fence():
    out = _validate('```json\n{"intent": "expense", "amount_paise": 100}\n```')
    assert out["intent"] == "expense"
    assert out["amount_paise"] == 100


def test_validate_defaults():
    out = _validate("{}")
    assert out["intent"] == "unknown"
    assert out["direction"] == "debit"
