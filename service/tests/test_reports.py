"""Tests for reports, search, and recurring reminders (Phase 6)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app import db, models
from app.config import settings
from app.ingest import ingest


@pytest.fixture
def setup(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/reports.db"
    db.reset_db()
    db.init_db()


def test_category_spend_groups(setup):
    from app.reports import category_spend, monthly_digest

    with db.session_scope() as s:
        ingest(s, "telegram", "dinner 900")
        ingest(s, "telegram", "lunch 300")
        ingest(s, "telegram", "groceries 1500")
        now = datetime.now()
        cats = category_spend(s, now.year, now.month)
        by_cat = {c["category"]: c["spend_paise"] for c in cats}
        assert by_cat.get("food") == 120000  # 900 + 300
        assert by_cat.get("groceries") == 150000

        digest = monthly_digest(s, now.year, now.month)
        assert digest["spend_paise"] == 270000
        assert digest["txn_count"] == 3
        assert digest["top_category"]["category"] == "groceries"


def test_search_finds_counterparty_and_category(setup):
    from app.reports import search_transactions

    with db.session_scope() as s:
        ingest(s, "telegram", "dinner 900")
        ingest(s, "telegram", "swiggy order 450")
        results = search_transactions(s, "swiggy")
        assert len(results) == 1
        results = search_transactions(s, "dinner")
        assert len(results) == 1


def test_due_recurring_preflag(setup):
    from app.reports import due_recurring

    with db.session_scope() as s:
        # Electric bill due on the 5th; today is the 10th -> not upcoming
        s.add(models.Recurring(
            pattern="monthly", expected_amount=200000,
            merchant="electric", day_of_month=5,
        ))
        # Rent due today + 2 days (12th) -> within horizon
        s.add(models.Recurring(
            pattern="monthly", expected_amount=1200000,
            merchant="rent", day_of_month=12,
        ))
        s.commit()
        rows = due_recurring(s, today=date(2026, 8, 10), horizon_days=3)
        assert [r.merchant for r in rows] == ["rent"]


def test_due_recurring_seen_this_month_skipped(setup):
    from app.reports import due_recurring

    with db.session_scope() as s:
        rec = models.Recurring(
            pattern="monthly", expected_amount=200000,
            merchant="electric", day_of_month=12,
            last_seen_at=datetime(2026, 8, 2),
        )
        s.add(rec)
        s.commit()
        rows = due_recurring(s, today=date(2026, 8, 10), horizon_days=3)
        assert rows == []


def test_due_recurring_clamps_invalid_day(setup):
    """Bug: day_of_month=31 crashed on a 30-day month. Clamp to month end."""
    from app.reports import due_recurring

    with db.session_scope() as s:
        s.add(models.Recurring(
            pattern="monthly", expected_amount=200000,
            merchant="netflix", day_of_month=31,
        ))
        s.commit()
        rows = due_recurring(s, today=date(2026, 4, 29), horizon_days=3)
        assert [r.merchant for r in rows] == ["netflix"]
