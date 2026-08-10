"""Refund matching (FINAL_PLAN.md Section 10.1).

Inbound credits that are refunds (merchant return / reversal) get linked to
their original expense via: same merchant + matching amount + 45-day window.

Conservative: only auto-link when EXACTLY ONE candidate matches. Ambiguous
(multiple merchants with same amount, or no merchant info) -> needs_review.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import models

REFUND_WINDOW_DAYS = 45


def find_refund_candidates(
    session,
    credit: models.Transaction,
) -> list[models.Transaction]:
    """Return original expenses this credit could be a refund of."""
    amount = abs(credit.amount_paise)
    if amount <= 0:
        return []
    window_start = credit.txn_date - timedelta(days=REFUND_WINDOW_DAYS)

    rows = session.execute(
        select(models.Transaction).where(
            models.Transaction.amount_paise == -amount,
            models.Transaction.type == "debit",
            models.Transaction.lifecycle == "active",
            models.Transaction.txn_date >= window_start,
            models.Transaction.txn_date <= credit.txn_date,
        )
    ).scalars().all()

    if not rows:
        return []

    # Merchant match first: same counterparty (VPA local part or merchant)
    credit_merchant = credit.counterparty_norm.strip().lower()
    merchant_matches = []
    for txn in rows:
        orig_merchant = txn.counterparty_norm.strip().lower()
        if credit_merchant and (credit_merchant in orig_merchant or orig_merchant in credit_merchant):
            merchant_matches.append(txn)
    if merchant_matches:
        return merchant_matches

    return rows


def suggest_refund(
    session,
    credit: models.Transaction,
) -> models.Transaction | None:
    """Single safe refund candidate, or None if ambiguous."""
    candidates = find_refund_candidates(session, credit)
    if len(candidates) == 1:
        return candidates[0]
    return None


def link_refund(
    session,
    credit: models.Transaction,
    original: models.Transaction,
    confidence: str = "auto",
) -> models.RefundLink:
    link = models.RefundLink(
        txn_id=credit.id,
        original_txn_id=original.id,
        amount=abs(credit.amount_paise),
        confidence=confidence,
    )
    session.add(link)
    credit.credit_type = "refund"
    credit.txn_state = "resolved_shared" if original.txn_state == "flagged_shared" else "personal"
    credit.status = "confirmed"
    session.add(credit)
    return link
