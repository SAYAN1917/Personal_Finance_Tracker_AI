"""Reconciliation (FINAL_PLAN.md Section 10.3, confidence ladder).

Confidence ladder: pending -> confirmed (2+ sources) -> verified (reconciled).

The core check: sum of tracked transactions for an account must reconcile to
the statement balance. Any diff is surfaced, never silently fixed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app import models


def ledger_delta(
    session,
    account: str,
    since: datetime | None = None,
) -> int:
    """Net paise movement of active txns for an account (debits negative).

    `since` limits to transactions after a date (used for incremental recon).
    """
    query = select(
        func.coalesce(func.sum(models.Transaction.amount_paise), 0)
    ).where(
        models.Transaction.account == account,
        models.Transaction.lifecycle == "active",
        models.Transaction.ownership == "mine",
    )
    if since is not None:
        query = query.where(models.Transaction.txn_date > since)
    return int(session.execute(query).scalar_one())


def reconcile_account(
    session,
    account_name: str,
    statement_balance_paise: int,
    as_of: datetime | None = None,
) -> dict:
    """Reconcile an account against a statement balance.

    Closing balance must equal the last verified (opening) balance plus the
    net movement of all tracked transactions since then. The diff is surfaced,
    never silently fixed. On match, the ladder advances to 'verified'.
    """
    as_of = as_of or datetime.now()

    account_row = session.execute(
        select(models.Account).where(models.Account.name == account_name)
    ).scalars().first()
    if account_row is None:
        account_row = models.Account(name=account_name, type="bank")
        session.add(account_row)

    opening = account_row.last_verified_balance_paise
    if opening is None:
        # First reconcile: no opening anchor to validate against, so this is a
        # baseline seed. The account's current statement balance becomes the
        # opening anchor for the next reconcile. Nothing gets verified yet.
        account_row.last_verified_balance_paise = statement_balance_paise
        account_row.last_reconciled_at = as_of
        return {
            "account": account_name,
            "opening_balance": None,
            "ledger_delta": ledger_delta(session, account_name),
            "expected_closing": None,
            "statement_balance": statement_balance_paise,
            "diff": None,
            "matched": True,
            "note": "Baseline seeded - next reconcile will validate the delta",
        }

    since = account_row.last_reconciled_at
    delta = ledger_delta(session, account_name, since)
    expected = opening + delta
    diff = statement_balance_paise - expected
    matched = diff == 0
    if matched:
        # Mark all active txns for the account as verified (ladder step 3)
        rows = session.execute(
            select(models.Transaction).where(
                models.Transaction.account == account_name,
                models.Transaction.lifecycle == "active",
                models.Transaction.txn_date <= as_of,
                models.Transaction.status != "verified",
            )
        ).scalars().all()
        for txn in rows:
            txn.status = "verified"
        account_row.last_verified_balance_paise = statement_balance_paise
        account_row.last_reconciled_at = as_of

    return {
        "account": account_name,
        "opening_balance": opening,
        "ledger_delta": delta,
        "expected_closing": expected,
        "statement_balance": statement_balance_paise,
        "diff": diff,
        "matched": matched,
        "note": "Reconciled" if matched else "Diff - do NOT verify; find the gap",
    }


def confidence_report(session) -> dict:
    """Count transactions by confidence state for the ladder."""
    states = ["pending", "confirmed", "verified"]
    counts = {}
    for state in states:
        counts[state] = session.execute(
            select(func.count(models.Transaction.id)).where(
                models.Transaction.status == state,
                models.Transaction.lifecycle == "active",
            )
        ).scalar_one()
    needs_review = session.execute(
        select(func.count(models.Transaction.id)).where(
            models.Transaction.needs_review == True  # noqa: E712
        )
    ).scalar_one()
    return {"status_counts": counts, "needs_review": needs_review}
