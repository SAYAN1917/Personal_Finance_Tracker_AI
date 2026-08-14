"""Tests for reconciliation and refund matching (Phase 5)."""

from __future__ import annotations

import pytest

from app import db, models
from app.config import settings
from app.ingest import ingest


@pytest.fixture
def setup(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/recon.db"
    db.reset_db()
    db.init_db()


def test_ledger_delta_sums_account(setup):
    from app.reconcile import ledger_delta

    with db.session_scope() as s:
        ingest(s, "telegram", "dinner 900")
        txn = s.query(models.Transaction).first()
        txn.account = "1234"
        assert ledger_delta(s, "1234") == -90000


def test_first_reconcile_seeds_baseline(setup):
    from app.reconcile import reconcile_account

    with db.session_scope() as s:
        ingest(s, "telegram", "dinner 900")
        txn = s.query(models.Transaction).first()
        txn.account = "1234"
        state = reconcile_account(s, "1234", 90000)
        assert state["matched"] is True
        assert state["note"].startswith("Baseline")
        s.commit()
        txn2 = s.get(models.Transaction, txn.id)
        assert txn2.status != "verified"  # baseline does not verify


def test_reconcile_incremental_matches_and_verifies(setup):
    from app.reconcile import reconcile_account

    with db.session_scope() as s:
        # Seed: opening balance 100000.
        state1 = reconcile_account(s, "1234", 100000)
        assert state1["matched"] is True
        s.commit()

        # Spend Rs 900 -> delta -90000; expected closing = 10000.
        ingest(s, "telegram", "dinner 900")
        txn = s.query(models.Transaction).first()
        txn.account = "1234"
        state2 = reconcile_account(s, "1234", 10000)
        assert state2["matched"] is True
        assert state2["opening_balance"] == 100000
        assert state2["ledger_delta"] == -90000
        s.commit()
        txn2 = s.get(models.Transaction, txn.id)
        assert txn2.status == "verified"


def test_reconcile_mismatch_not_verified(setup):
    from app.reconcile import reconcile_account

    with db.session_scope() as s:
        reconcile_account(s, "1234", 100000)  # seed
        s.commit()
        ingest(s, "telegram", "dinner 900")
        txn = s.query(models.Transaction).first()
        txn.account = "1234"
        # Wrong statement balance: expected 10000, we claim 20000.
        state = reconcile_account(s, "1234", 20000)
        assert state["matched"] is False
        assert state["diff"] == 10000
        s.commit()
        txn2 = s.get(models.Transaction, txn.id)
        assert txn2.status != "verified"


def test_refund_single_candidate_auto_links(setup):
    with db.session_scope() as s:
        ingest(s, "telegram", "swiggy order 450")
        original = s.query(models.Transaction).first()
        assert original.type == "debit"

        res = ingest(
            s,
            "telegram",
            "Rs.450.00 credited from swiggy refund",
        )
        credit = res.transaction
        assert res.refund_link is not None
        assert credit.credit_type == "refund"

        links = s.query(models.RefundLink).all()
        assert len(links) == 1
        assert links[0].original_txn_id == original.id


def test_refund_ambiguous_no_auto_link(setup):
    with db.session_scope() as s:
        ingest(s, "telegram", "swiggy order 450")
        ingest(s, "telegram", "zomato order 450")
        # no merchant in credit text -> two same-amount candidates -> ambiguous
        res = ingest(
            s,
            "telegram",
            "Rs.450.00 credited from unknownvendor on 05-08-26",
        )
        assert res.refund_link is None
        assert s.query(models.RefundLink).count() == 0


def test_confidence_report_counts(setup):
    from app.reconcile import confidence_report

    with db.session_scope() as s:
        ingest(s, "telegram", "dinner 900")
        report = confidence_report(s)
        assert report["status_counts"]["pending"] >= 1
