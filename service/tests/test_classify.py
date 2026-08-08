"""Tests for the classifier - known-persons first, whole-word regex (the
ravi@ybl -> 'bills' bug)."""

from app.classify import classify_category, is_known_person, is_shared_heuristic


def test_known_person_checked_first():
    assert is_known_person("ravi", ["ravi", "sam"]) is True
    assert is_known_person("ravi", ["sam"]) is False
    result = classify_category("ravi", known_persons=["ravi", "sam"])
    assert result == "shared"


def test_ravi_never_matches_bills():
    # Prototype bug: 'vi' inside 'ravi' matched telecom brand Vi -> bills
    result = classify_category("ravi")
    assert result != "bills"
    assert result is None


def test_vi_matches_bills_as_word():
    # 'vi' as its own word (telecom brand) still maps to bills
    assert classify_category("vi") == "bills"
    assert classify_category("vodafone") == "bills"


def test_merchant_alias_map():
    assert classify_category("swiggy") == "food"
    assert classify_category("zomato") == "food"
    assert classify_category("amazon pay") == "shopping"


def test_category_keywords_whole_word():
    assert classify_category("dinner") == "food"
    assert classify_category("movies") == "entertainment"


def test_unknown_counterparty():
    assert classify_category("randomshopxyz") is None


def test_shared_heuristics():
    # odd amount -> suspect
    assert is_shared_heuristic(74900, "anyone", None, 500) is True
    # known person -> suspect
    assert is_shared_heuristic(30000, "sam", None, 500, known_persons=["sam"]) is True
    # shared category -> suspect
    assert is_shared_heuristic(40000, "swiggy", "food", 500) is True
    # above threshold -> suspect
    assert is_shared_heuristic(80000, "johndoe", None, 500) is True
    # small, rounded, unknown, no category -> personal
    assert is_shared_heuristic(45000, "unknownshop", None, 500) is False
