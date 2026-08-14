"""Tests for shared expenses, group expenses, and settlement matching."""

import pytest

from app import db, models
from app.config import settings
from app.ingest import ingest
from app.settlements import apply_settlement, suggest_settlement


@pytest.fixture()
def session(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/settle.db"
    db.reset_db()
    db.init_db()
    Session = db.get_sessionmaker()
    s = Session()
    yield s
    s.close()


def _create_group(session, txn, person="sam", share_paise=None):
    full = abs(txn.amount_paise)
    receivable = max(0, full - share_paise) if share_paise else 0
    ge = models.GroupExpense(
        transaction_id=txn.id,
        person=person,
        full_amount=full,
        share_amount=share_paise,
        expected_receivable=receivable,
        status="open",
    )
    session.add(ge)
    session.commit()
    return ge


def test_flag_shared_then_create_group(session):
    r = ingest(session, "telegram", "dinner 900")
    session.commit()
    txn = r.transaction
    assert txn.txn_state == "personal"

    # Simulate bot answer: Yes, shared
    txn.txn_state = "flagged_shared"
    session.commit()

    # Flag != receivable: no group expense yet -> no settlement candidate
    ge = _create_group(session, txn, "sam", share_paise=30000)
    assert ge.expected_receivable == 60000  # 900 - 300


def test_settlement_matches_single_candidate(session):
    r = ingest(session, "telegram", "dinner 900")
    session.commit()
    _create_group(session, r.transaction, "sam", share_paise=30000)

    # Inbound credit from sam
    inc = ingest(
        session,
        "sms",
        "Rs.600.00 credited to A/c **1234 on 05-08-26 by UPI: sam@ybl. Ref: 412345678998",
    )
    session.commit()
    assert inc.transaction.type == "credit"

    candidate = suggest_settlement(session, inc.transaction.amount_paise, "sam")
    assert candidate is not None
    assert candidate.person == "sam"

    state = apply_settlement(session, inc.transaction, candidate)
    session.commit()
    assert state["status"] == "settled"
    assert inc.transaction.credit_type == "reimbursement"
    assert inc.transaction.txn_state == "resolved_shared"


def test_ambiguous_settlement_left_unmatched(session):
    """Prototype Bug B: two groups expecting the same amount -> no auto-match."""
    r1 = ingest(session, "telegram", "dinner 900")
    session.commit()
    _create_group(session, r1.transaction, "sam", share_paise=30000)

    r2 = ingest(session, "telegram", "lunch 600")
    session.commit()
    # second group with same expected receivable 600
    _create_group(session, r2.transaction, "ravi", share_paise=0)
    # ravi group expected_receivable is 0 (flag-only) -> force 600 for ambiguity
    ge = session.query(models.GroupExpense).filter_by(person="ravi").first()
    ge.expected_receivable = 60000
    session.commit()

    ingest(
        session,
        "sms",
        "Rs.600.00 credited to A/c **1234 on 05-08-26 by UPI: sam@ybl. Ref: 412345678998",
    )
    session.commit()
    # sam matched by person (score 2.0); ravi only by amount (1.0) -> sam wins,
    # not ambiguous. Force a truly ambiguous case: unknown sender, two by amount.
    inc2 = ingest(
        session,
        "sms",
        "Rs.600.00 credited to A/c **1234 on 06-08-26 by UPI: stranger@ybl. Ref: 412345678997",
    )
    session.commit()
    candidate = suggest_settlement(session, inc2.transaction.amount_paise, "stranger")
    assert candidate is None  # ambiguous - both sam and ravi expect 600


def test_partial_settlement(session):
    r = ingest(session, "telegram", "dinner 900")
    session.commit()
    ge = _create_group(session, r.transaction, "sam", share_paise=30000)

    inc = ingest(
        session,
        "sms",
        "Rs.300.00 credited to A/c **1234 on 05-08-26 by UPI: sam@ybl. Ref: 412345678996",
    )
    session.commit()
    state = apply_settlement(session, inc.transaction, ge, amount_paise=30000)
    session.commit()
    assert state["status"] == "partial"
    assert state["outstanding_after"] == 30000


def test_group_expense_share_unknown_is_deferred(session):
    """Bug: share=None was treated as 'friend owes the full amount'. Flag-only
    means deferred math (Section 7.3): receivable stays 0 until settlement."""
    from app.settlements import create_group_expense

    r = ingest(session, "telegram", "dinner 900")
    session.commit()
    ge = create_group_expense(session, r.transaction, "sam", None)
    session.commit()
    assert ge.share_amount is None
    assert ge.expected_receivable == 0
    assert ge.status == "open"


def test_settlement_empty_fields_no_false_person_match(session):
    """Bug: operator precedence let an empty counterparty falsely match an
    empty-person group expense."""
    from app.settlements import find_settlement_candidates

    r = ingest(session, "telegram", "dinner 900")
    session.commit()
    _create_group(session, r.transaction, "", share_paise=30000)
    candidates = find_settlement_candidates(session, 99900, "")
    assert candidates == []
