"""Ingestion pipeline: event log -> parse -> dedup -> classify -> persist.

Implements FINAL_PLAN.md pipeline: ingest -> parse -> normalize -> dedup ->
settlement-match -> credit-type -> category (Section 6).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select

from app import models
from app.classify import classify_category, is_shared_heuristic
from app.dedup import CHANNEL_PRIORITY, DedupEngine
from app.parser import PARSERS, ParsedTxn
from app.settlements import suggest_settlement

logger = logging.getLogger(__name__)


class IngestResult:
    def __init__(
        self,
        outcome: str,
        transaction: models.Transaction | None = None,
        message: str = "",
        needs_review: bool = False,
        settlement_candidate: models.GroupExpense | None = None,
    ):
        self.outcome = outcome  # NEW / EXACT / STRONG / WEAK / FAILED
        self.transaction = transaction
        self.message = message
        self.needs_review = needs_review
        self.settlement_candidate = settlement_candidate


def log_event(session, source: str, text: str, channel: str = "") -> models.Event:
    event = models.Event(
        source=source,
        channel=channel or source,
        raw_payload=text,
    )
    session.add(event)
    return event


def _resolve_known_persons(session) -> list[str]:
    rows = session.execute(select(models.Person)).scalars().all()
    names = []
    for p in rows:
        names.append(p.name)
        try:
            names.extend(json.loads(p.aliases))
        except (ValueError, TypeError):
            pass
    return names


def ingest(session, source: str, text: str, channel: str = "") -> IngestResult:
    log_event(session, source, text, channel)

    parser_fn = PARSERS.get(source) or PARSERS["telegram"]
    parsed: ParsedTxn = parser_fn(text)
    if parsed.confidence < 0.3:
        return IngestResult(
            "FAILED",
            message="Could not parse: " + text[:80],
            needs_review=True,
        )

    engine = DedupEngine(session)
    match = engine.match(parsed, channel)

    if match.outcome in ("EXACT", "STRONG"):
        canonical = match.canonical
        engine.merge(parsed, channel or source, canonical)
        session.add(canonical)
        # Classification may still apply for an inbound credit (settlement)
        result = IngestResult(
            "EXACT" if match.outcome == "EXACT" else "STRONG",
            transaction=canonical,
            message=f"Duplicate of #{canonical.id}",
        )
        _handle_inbound_credit(session, parsed, canonical, channel, result)
        return result

    if match.outcome == "WEAK":
        return IngestResult(
            "WEAK",
            message="Possible duplicate - needs review",
            needs_review=True,
        )

    # NEW transaction
    txn = models.Transaction(
        txn_date=parsed.txn_date or datetime.now(),
        value_date=parsed.txn_date,
        amount_paise=parsed.amount_paise,
        currency=parsed.currency,
        type=parsed.txn_type,
        mode=parsed.mode,
        counterparty_norm=parsed.counterparty,
        account=parsed.account,
        utr=parsed.utr,
        sources=json.dumps([channel or source]),
        parser_version=parsed.parser_version,
        status="pending",
        lifecycle="active",
        txn_state="personal",
        ownership="mine",
        needs_review=False,
    )

    if parsed.utr:
        from app.normalizer import exact_key

        txn.key_exact = exact_key(
            parsed.amount_paise,
            parsed.txn_date,
            parsed.counterparty,
            parsed.mode,
            parsed.account,
            parsed.utr,
        )
    else:
        from app.normalizer import fingerprint_key

        txn.key_soft = fingerprint_key(
            parsed.amount_paise,
            parsed.txn_date,
            parsed.counterparty,
            parsed.mode,
            parsed.account,
        )

    session.add(txn)
    session.flush()  # assign id

    # Pipeline: settlement-match -> credit-type -> category
    result = IngestResult("NEW", transaction=txn)
    _classify_new(session, parsed, txn, result)
    _handle_inbound_credit(session, parsed, txn, channel, result)
    return result


def _classify_new(session, parsed: ParsedTxn, txn: models.Transaction, result: IngestResult):
    """Apply credit-type and category to a NEW transaction."""
    known_persons = _resolve_known_persons(session)

    if parsed.txn_type == "credit":
        txn.credit_type = "income"
        txn.category = "income"
        result.message = "Incoming credit (income/reimbursement?)"
        return

    category = classify_category(parsed.counterparty, known_persons)
    txn.category = category
    result.message = f"New expense: Rs {abs(txn.amount_paise) / 100:.0f}"


def _handle_inbound_credit(session, parsed, txn, channel, result: IngestResult):
    """Inbound credits are checked against open group expenses FIRST."""
    if parsed.txn_type != "credit":
        return

    candidate = suggest_settlement(session, parsed.amount_paise, parsed.counterparty)

    if candidate:
        result.settlement_candidate = candidate
        result.message = (
            f"Incoming credit possibly settles group expense #{candidate.id} "
            f"({candidate.person}) - confirm"
        )
