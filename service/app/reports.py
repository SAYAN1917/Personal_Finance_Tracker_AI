"""Reports and reminders (FINAL_PLAN.md Section 12, Phase 6).

- Category breakdown for a month (recurring vs one-off).
- Monthly digest summary.
- Recurring bill reminders (pre-flag based on day_of_month + last_seen).
- /find search.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from app import models


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end


def category_spend(
    session,
    year: int,
    month: int,
    exclude_recurring: bool = False,
) -> list[dict]:
    """Spend by category for a month. Debits only, active, mine."""
    start, end = _month_range(year, month)
    rows = session.execute(
        select(models.Transaction.category, func.sum(models.Transaction.amount_paise))
        .where(
            models.Transaction.type == "debit",
            models.Transaction.lifecycle == "active",
            models.Transaction.ownership == "mine",
            models.Transaction.txn_date >= start,
            models.Transaction.txn_date < end,
            models.Transaction.category.isnot(None),
        )
        .group_by(models.Transaction.category)
    ).all()
    return [
        {"category": cat, "spend_paise": -int(total)}
        for cat, total in rows
        if cat and total
    ]


def monthly_digest(session, year: int, month: int) -> dict:
    """Total spend, income, count, top category for a month."""
    start, end = _month_range(year, month)
    spend = session.execute(
        select(func.coalesce(func.sum(models.Transaction.amount_paise), 0)).where(
            models.Transaction.type == "debit",
            models.Transaction.lifecycle == "active",
            models.Transaction.ownership == "mine",
            models.Transaction.txn_date >= start,
            models.Transaction.txn_date < end,
        )
    ).scalar_one()
    income = session.execute(
        select(func.coalesce(func.sum(models.Transaction.amount_paise), 0)).where(
            models.Transaction.type == "credit",
            models.Transaction.lifecycle == "active",
            models.Transaction.ownership == "mine",
            models.Transaction.txn_date >= start,
            models.Transaction.txn_date < end,
        )
    ).scalar_one()
    count = session.execute(
        select(func.count(models.Transaction.id)).where(
            models.Transaction.type == "debit",
            models.Transaction.lifecycle == "active",
            models.Transaction.ownership == "mine",
            models.Transaction.txn_date >= start,
            models.Transaction.txn_date < end,
        )
    ).scalar_one()

    top = category_spend(session, year, month)
    top_category = max(top, key=lambda c: c["spend_paise"]) if top else None

    return {
        "month": f"{year:04d}-{month:02d}",
        "spend_paise": -int(spend),
        "income_paise": int(income),
        "net_paise": int(income) + int(spend),
        "txn_count": count,
        "top_category": top_category,
        "categories": top,
    }


def search_transactions(session, query: str, limit: int = 10) -> list[models.Transaction]:
    q = f"%{query}%"
    return list(
        session.execute(
            select(models.Transaction)
            .where(
                (models.Transaction.counterparty_norm.ilike(q))
                | (models.Transaction.category.ilike(q))
                | (models.Transaction.utr.ilike(q))
            )
            .order_by(models.Transaction.txn_date.desc())
            .limit(limit)
        ).scalars().all()
    )


def due_recurring(
    session,
    today: date | None = None,
    horizon_days: int = 3,
) -> list[models.Recurring]:
    """Recurring bills due within the next horizon days (pre-flag)."""
    today = today or date.today()
    upcoming = []
    for rec in session.execute(
        select(models.Recurring).where(models.Recurring.active == True)  # noqa: E712
    ).scalars().all():
        if rec.day_of_month is None:
            continue
        last = rec.last_seen_at
        # Skip if we already saw it this cycle (seen this month)
        if last is not None and last.month == today.month and last.year == today.year:
            continue
        due = date(today.year, today.month, rec.day_of_month)
        if due < today:
            # already passed this month - not an upcoming reminder
            continue
        if (due - today).days <= horizon_days:
            upcoming.append(rec)
    return upcoming


def mark_recurring_seen(session, rec: models.Recurring, txn: models.Transaction):
    """Link a recurring bill to a transaction and mark it seen."""
    rec.last_seen_at = txn.txn_date
    if txn.category is None:
        txn.category = rec.category or "bills"
    session.add(rec)
    session.add(txn)
