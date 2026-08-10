"""Per-bank parsers (FINAL_PLAN.md Section 4.1).

Each parser returns a normalized dict with a confidence score. Raw text is
always retained by the caller (event log). Parser version is recorded per row
for source-format-drift diagnosis (edge case 25).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.normalizer import (
    extract_utr,
    merchant_from_text,
    normalize_counterparty,
    parse_amount,
)

_PARSER_VERSION = 1


@dataclass
class ParsedTxn:
    txn_date: datetime | None = None
    amount_paise: int = 0
    currency: str = "INR"
    txn_type: str = "debit"  # debit/credit
    mode: str = "UPI"
    counterparty: str = ""
    account: str = ""
    utr: str | None = None
    confidence: float = 0.0
    parser_version: int = _PARSER_VERSION
    flags: list[str] = field(default_factory=list)


_DATE_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})")
_DATE_LONG_RE = re.compile(r"(\d{1,2})\s+([a-z]{3})\s+(\d{2,4})", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    for match in _DATE_RE.finditer(text):
        d, m, y = match.groups()
        year = int(y)
        if year < 100:
            year += 2000 if year < 50 else 1900
        try:
            return datetime(year, int(m), int(d))
        except ValueError:
            continue
    for match in _DATE_LONG_RE.finditer(text):
        d, mon, y = match.groups()
        mon_num = _MONTHS.get(mon[:3].lower())
        if not mon_num:
            continue
        year = int(y)
        if year < 100:
            year += 2000 if year < 50 else 1900
        try:
            return datetime(year, mon_num, int(d))
        except ValueError:
            continue
    return None


def _detect_mode(text: str) -> str:
    if re.search(r"\bupi\b", text, re.IGNORECASE):
        return "UPI"
    if re.search(r"\b(neft|imps)\b", text, re.IGNORECASE):
        return "NEFT"
    if re.search(r"\bcard\b|\bpos\b|purchase at", text, re.IGNORECASE):
        return "CARD"
    return "UPI"


def _detect_account(text: str) -> str:
    match = re.search(r"\*{1,2}(\d{4})", text)
    if match:
        return match.group(1)
    match = re.search(r"a/c[:\s]*(\*?\d{4})", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def parse_upi_sms(text: str, now: datetime | None = None) -> ParsedTxn:
    """Generic UPI SMS parser (works across banks for the common format)."""
    result = ParsedTxn()
    result.utr = extract_utr(text)
    amount = parse_amount(text)  # direction auto-detected from words (Bug D)
    if amount is None:
        result.confidence = 0.2
        result.flags.append("no_amount")
        return result
    result.amount_paise, result.currency = amount
    result.txn_type = "debit" if result.amount_paise < 0 else "credit"

    result.txn_date = _parse_date(text, now)
    result.mode = _detect_mode(text)
    result.account = _detect_account(text)
    result.counterparty = merchant_from_text(text)
    result.confidence = 0.7 if result.amount_paise and result.utr else 0.5
    return result


def parse_card_pdf(text: str, now: datetime | None = None) -> ParsedTxn:
    """Card statement line parser - usually no UTR (fuzzy path)."""
    result = ParsedTxn()
    result.utr = extract_utr(text) or None
    result.txn_date = _parse_date(text, now)
    amount = parse_amount(text, direction_hint=None)
    if amount is None:
        result.confidence = 0.2
        result.flags.append("no_amount")
        return result
    result.amount_paise, result.currency = amount
    result.txn_type = "debit" if result.amount_paise < 0 else "credit"
    result.mode = "CARD"
    result.counterparty = merchant_from_text(text) or normalize_counterparty(
        re.sub(r"^\d+\s*", "", text)[:40]
    )
    result.confidence = 0.6
    return result


def parse_gpay_csv(line: str, now: datetime | None = None) -> ParsedTxn:
    """GPay statement CSV row. Format varies; extract by common column keywords."""
    result = ParsedTxn()
    parts = [p.strip() for p in line.split(",")]
    joined = " ".join(parts)

    result.utr = extract_utr(joined) or None
    result.txn_date = _parse_date(joined, now)

    amount_match = re.search(r"(?:Debit|Credit)?\s*(?:Rs\.?\s*)?(\d+\.?\d*)", joined, re.IGNORECASE)
    if amount_match:
        result.amount_paise = round(float(amount_match.group(1)) * 100)

    if re.search(r"\bdebit\b", joined, re.IGNORECASE):
        result.txn_type = "debit"
        result.amount_paise = -abs(result.amount_paise)
    elif re.search(r"\bcredit\b", joined, re.IGNORECASE):
        result.txn_type = "credit"
        result.amount_paise = abs(result.amount_paise)
    result.mode = "UPI"
    result.counterparty = merchant_from_text(joined)
    result.confidence = 0.6 if result.amount_paise else 0.2
    return result


def parse_telegram_entry(text: str, now: datetime | None = None) -> ParsedTxn:
    """Free-text manual entry: 'dinner 450' or 'group dinner 900 share 300'.

    This is the deterministic fallback. The NLU AI lane (Section 6.3) can parse
    richer phrasing, but this must work standalone.
    """
    result = ParsedTxn()

    # Direction from words (Bug D): credited/received = credit, else debit
    if re.search(r"\b(credited|received|came in|refund)\b", text, re.IGNORECASE):
        result.txn_type = "credit"
    else:
        result.txn_type = "debit"

    amount = parse_amount(text, direction_hint=None if result.txn_type == "credit" else "debit")
    if amount:
        result.amount_paise, result.currency = amount
    else:
        amount_match = re.search(r"(\d+(?:\.\d{1,2})?)", text)
        if amount_match:
            result.amount_paise = round(float(amount_match.group(1)) * 100)
        result.currency = "INR"
    if result.txn_type == "credit":
        result.amount_paise = abs(result.amount_paise)
    else:
        result.amount_paise = -abs(result.amount_paise)

    result.mode = "UPI"
    # Counterparty: 'from X' phrase wins; otherwise first words after amount
    from_match = re.search(r"\bfrom\s+([a-zA-Z][a-zA-Z0-9_.]*)\b", text, re.IGNORECASE)
    if from_match:
        result.counterparty = normalize_counterparty(from_match.group(1))
    else:
        rest = re.sub(r"[\d.,\s]+", " ", text).strip()
        result.counterparty = normalize_counterparty(" ".join(rest.split()[:2]))
    result.txn_date = _parse_date(text, now)
    result.confidence = 0.8 if result.amount_paise else 0.3
    return result


PARSERS = {
    "sms": parse_upi_sms,
    "email": parse_upi_sms,
    "card_pdf": parse_card_pdf,
    "upi_csv": parse_gpay_csv,
    "telegram": parse_telegram_entry,
}
