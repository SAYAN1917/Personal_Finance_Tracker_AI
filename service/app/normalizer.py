"""Normalization rules (FINAL_PLAN.md Section 4.2).

Amounts -> integer paise (never floats). Direction from surrounding words on
bare amounts (Prototype Bug D). UTR extraction requires a ref/utr/upi label in
context (Prototype Bug E). Counterparty stripping of UPI/VPA suffixes.
"""

import hashlib
import re

_RS_RE = re.compile(r"rs\.?\s?|inr\s?|₹|uah\s?|usd\s?", re.IGNORECASE)

# Direction words - must appear before/after a bare number to assign debit/credit
_DEBIT_WORDS = re.compile(
    r"\b(debited|debit|spent|paid|dr|sent|purchased|withdrawn)\b", re.IGNORECASE
)
_CREDIT_WORDS = re.compile(
    r"\b(credited|credit|cr|received|refund|deposited|cashback|reward|repayment)\b",
    re.IGNORECASE,
)

# A UTR / reference must carry a label; a bare 12-digit number is NOT a UTR
_UTR_PATTERNS = [
    re.compile(r"\b(?:ref|reference|utr|txn id|transaction id|ref no)[:\s.]+([0-9]{10,32})", re.IGNORECASE),
    re.compile(r"\bupi[/:][\s:]*(?:[0-9]{4,12}/)?([0-9]{10,32})", re.IGNORECASE),
]

_AMOUNT_RE = re.compile(
    r"(?:rs\.?\s*|inr\s*|₹|uah\s*|usd\s*)((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)|((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)\s?(?:rs\.?|inr|₹)?",
    re.IGNORECASE,
)


def parse_amount(text: str, direction_hint: str | None = None) -> tuple[int, str] | None:
    """Extract amount as signed paise. Returns (amount_paise, currency) or None.

    direction_hint is an explicit 'debit'/'credit' supplied by the caller.
    Bare amounts (no currency symbol) get their sign from surrounding words
    (Prototype Bug D): '450.00 debited' -> debit, '500 received' -> credit.
    """
    # Mask labeled UTRs first: "Ref: 999900001111" must never become an amount
    text = _mask_utrs(text)

    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    digits = match.group(1) or match.group(2)
    if not digits:
        return None
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    paise = round(value * 100)

    currency = "INR"
    amount_str = match.group(0)
    upper = amount_str.upper()
    if "USD" in upper:
        currency = "USD"
    elif "UAH" in upper:
        currency = "UAH"

    if direction_hint == "credit":
        return paise, currency
    if direction_hint == "debit":
        return -paise, currency

    # No explicit hint - look for direction words in the text
    if _CREDIT_WORDS.search(text):
        return paise, currency
    if _DEBIT_WORDS.search(text):
        return -paise, currency

    # Ambiguous bare amount with no direction word anywhere: default debit is
    # dangerous; return positive and let the caller flag needs_review.
    return paise, currency


def extract_utr(text: str) -> str | None:
    """Return a validated UTR, or None. Requires a ref/utr/upi label (Bug E)."""
    for pattern in _UTR_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _mask_utrs(text: str) -> str:
    """Replace labeled UTRs with spaces so they can't be parsed as amounts."""
    masked = text
    for pattern in _UTR_PATTERNS:
        masked = pattern.sub(" ", masked)
    return masked


def normalize_counterparty(raw: str) -> str:
    """Strip VPA suffixes, UPI/ prefix, and collapse case (Section 4.2)."""
    if not raw:
        return ""
    s = raw.strip().lower()
    s = re.sub(r"^upi[/:]", "", s)
    # VPA: local part before @
    if "@" in s:
        s = s.split("@")[0]
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def merchant_from_text(text: str) -> str:
    """Heuristic extraction of a merchant/person name from a txn message."""
    # After 'by UPI: xxx' or 'to xxx' / 'at xxx'
    patterns = [
        re.compile(r"by\s+upi[/:]?\s*([a-z0-9_.@]+)", re.IGNORECASE),
        re.compile(r"\bat\s+([a-z][a-z0-9\s]{1,30})", re.IGNORECASE),
        re.compile(r"\bto\s+([a-z][a-z0-9\s]{1,30})", re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return normalize_counterparty(match.group(1))
    return ""


def fingerprint_key(amount_paise: int, txn_date, counterparty: str, mode: str = "", account: str = "") -> str:
    """Tier 2 soft key: signed amount + account + counterparty (Bug C)."""
    date_str = txn_date.strftime("%Y-%m-%d") if txn_date else ""
    raw = f"{amount_paise}|{account}|{counterparty}|{date_str}|{mode}"
    return hashlib.sha256(raw.encode()).hexdigest()


def exact_key(
    amount_paise: int,
    txn_date,
    counterparty: str,
    mode: str,
    account: str,
    utr: str,
) -> str:
    """Tier 1 exact key: signed amount + account + date + counterparty + mode + UTR."""
    date_str = txn_date.strftime("%Y-%m-%d") if txn_date else ""
    raw = f"{amount_paise}|{account}|{date_str}|{counterparty}|{mode}|{utr}"
    return hashlib.sha256(raw.encode()).hexdigest()
