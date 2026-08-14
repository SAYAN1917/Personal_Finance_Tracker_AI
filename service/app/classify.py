"""Classification (FINAL_PLAN.md Section 6).

Order: known-persons -> merchant alias map -> keyword regex with whole-word
anchors both sides. Credit typing happens BEFORE categorization (Section 6.1),
so inbound money is never categorized as spend.
"""

from __future__ import annotations

import re

# Merchant alias map (Section 6.2). Keys and values normalized lowercase.
MERCHANT_ALIASES = {
    "swiggy": "food",
    "zomato": "food",
    "dominos": "food",
    "domino": "food",
    "kfc": "food",
    "mcdonald": "food",
    "mcdonalds": "food",
    "pizzahut": "food",
    "bigbasket": "groceries",
    "grofers": "groceries",
    "blinkit": "groceries",
    "instamart": "groceries",
    "dmart": "groceries",
    "amazon pay": "shopping",
    "amazon": "shopping",
    "flipkart": "shopping",
    "myntra": "shopping",
    "ajio": "shopping",
    "paytm": "wallet",
    "phonepe": "wallet",
    "google pay": "wallet",
    "gpay": "wallet",
    "uber": "transport",
    "ola": "transport",
    "rapido": "transport",
    "irctc": "transport",
    "redbus": "transport",
    "petrol": "transport",
    "indian oil": "transport",
    "hpcl": "transport",
    "bharat petroleum": "transport",
    "jio": "bills",
    "airtel": "bills",
    "vodafone": "bills",
    "vi": "bills",
    "electricity": "bills",
    "bses": "bills",
    "tata power": "bills",
    "netflix": "subscriptions",
    "spotify": "subscriptions",
    "prime": "subscriptions",
    "hotstar": "subscriptions",
    "sony liv": "subscriptions",
    "ze5": "subscriptions",
    "cred": "fees",
    "slice": "fees",
}

# Category keywords - whole-word anchors on BOTH sides (Section 6.2 rule 3).
CATEGORY_KEYWORDS = [
    (r"\b(food|restaurant|dinner|lunch|breakfast|cafe|coffee|hotel)\b", "food"),
    (r"\b(grocer(y|ies)|vegetable|market|supermarket)\b", "groceries"),
    (r"\b(taxi|cab|uber|ola|metro|train|fuel|petrol|parking|toll)\b", "transport"),
    (r"\b(movies?|cinema|theatres?|games?|concerts?)\b", "entertainment"),
    (r"\b(medical|pharmacy|hospital|doctor|clinic|medicine)\b", "health"),
    (r"\b(rent|maintenance|society)\b", "rent"),
    (r"\b(electricity|water|gas|wifi|internet|phone|mobile)\b", "bills"),
    (r"\b(subscription|netflix|spotify|prime|premium)\b", "subscriptions"),
    (r"\b(clothes|shoes|apparel|fashion)\b", "shopping"),
    (r"\b(gift|present|donation)\b", "gifts"),
    (r"\b(salary|interest|dividend)\b", "income"),
]

# Transfers (Section 6 edge cases 19/20/24): moving money between your own
# accounts/pockets is NOT spend. Whole-word anchored; conservative.
TRANSFER_PATTERNS = [
    r"\b(wallet top\s?-?up|add money to wallet|wallet recharge)\b",
    r"\b(paytm wallet|phonepe wallet|amazon pay balance)\b",
    r"\b(credit card (bill|repayment|payment)|card bill|credit card bill payment)\b",
    r"\b(transfer to self|self transfer|own account|own a/c|between accounts|my other account)\b",
]


def is_known_person(counterparty: str, persons: list[str]) -> bool:
    if not counterparty:
        return False
    c = counterparty.strip().lower()
    return any(p.strip().lower() == c for p in persons)


def classify_category(counterparty: str, known_persons: list[str] | None = None) -> str | None:
    """Returns category or None if unknown. known_persons list checked first."""
    known_persons = known_persons or []
    c = counterparty.strip().lower()

    if is_known_person(counterparty, known_persons):
        return "shared"

    for alias, category in MERCHANT_ALIASES.items():
        if len(alias) <= 3:
            # short aliases must match as whole words - 'vi' must not match 'ravi'
            if re.search(rf"\b{re.escape(alias)}\b", c):
                return category
        elif alias in c:
            return category

    for pattern, category in CATEGORY_KEYWORDS:
        if re.search(pattern, c):
            return category

    return None


def is_transfer(counterparty: str) -> bool:
    """True if the debit moves money between own accounts/pockets (wallet
    top-up, card-bill repayment, self-transfer). Such debits are tagged
    category 'transfer' and excluded from spend (Section 6 edge cases 19/20)."""
    if not counterparty:
        return False
    c = counterparty.strip().lower()
    return any(re.search(pattern, c) for pattern in TRANSFER_PATTERNS)


def is_shared_heuristic(
    amount_paise: int,
    counterparty: str,
    category: str | None,
    threshold: int,
    known_persons: list[str] | None = None,
) -> bool:
    """Trigger-driven prompt heuristics (Section 7.3)."""
    amount = abs(amount_paise) / 100
    if amount <= 0:
        return False
    # Odd/non-rounded amounts
    if not float(amount).is_integer():
        return True
    if amount % 1 != 0:
        return True
    if known_persons and is_known_person(counterparty, known_persons):
        return True
    if category in ("shared", "food", "groceries", "entertainment", "rent", "transport"):
        return True
    if amount >= threshold:
        return True
    return False
