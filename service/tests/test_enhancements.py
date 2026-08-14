"""Regression tests for the Phase-8 on-top enhancements:

A. confirm-merge (WEAK resolution)
B. refund reduces group-expense receivable
C. recurring bill auto-link on ingest
D. known-persons auto-learning
E. transfer detection excluded from spend
F. audit log writes
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, models
from app.classify import is_transfer
from app.config import settings
from app.dedup import merge_transaction
from app.ingest import ingest
from app.main import app
from app.refunds import link_refund
from app.settlements import create_group_expense, learn_person


@pytest.fixture()
def session(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/test.db"
    db.reset_db()
    db.init_db()
    Session = db.get_sessionmaker()
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def client_and_session(tmp_path):
    """Shared DB so API calls and direct session queries see the same rows."""
    settings.database_url = f"sqlite:///{tmp_path}/api.db"
    settings.webhook_secret = "test-secret"
    db.reset_db()
    db.init_db()
    Session = db.get_sessionmaker()
    s = Session()
    with TestClient(app) as c:
        yield c, s
    s.close()


def _headers():
    return {"X-Webhook-Secret": "test-secret"}


def _sms_swiggy(day: str = "03-08-26", ref: str = "412345678999"):
    return f"Rs.900.00 debited from A/c **1234 on {day} by UPI: swiggy. Ref: {ref}"


def _sms_date_drift():
    # same amount + merchant, 3 days later, different UTR -> WEAK (Bug F)
    return _sms_swiggy(day="06-08-26", ref="777000000777")


# ---------- A. confirm-merge ----------

def test_weak_ingest_exposes_duplicate_of(session):
    ingest(session, "sms", _sms_swiggy())
    session.commit()
    r2 = ingest(session, "email", _sms_date_drift())
    session.commit()
    assert r2.outcome == "WEAK"
    assert r2.duplicate_of is not None
    assert r2.transaction.needs_review is True


def test_merge_transaction_folds_and_removes(session):
    """[Merge]: incoming WEAK txn folds into canonical and is deleted."""
    canonical = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    weak = ingest(session, "email", _sms_date_drift())
    session.commit()
    incoming = weak.transaction

    merged = merge_transaction(session, incoming, canonical)
    session.commit()

    assert merged.id == canonical.id
    assert session.get(models.Transaction, incoming.id) is None
    assert "email" in canonical.sources
    assert canonical.status == "confirmed"  # 2 sources
    dm = session.execute(
        select(models.DedupMap).where(models.DedupMap.canonical_id == canonical.id)
    ).scalars().first()
    assert dm is not None and dm.raw_id == str(incoming.id)


def test_confirm_merge_endpoint_keep(client_and_session):
    client, session = client_and_session
    canonical = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    weak = ingest(session, "email", _sms_date_drift())
    session.commit()
    incoming_id = weak.transaction.id

    resp = client.post(
        "/webhook/confirm-merge",
        json={"incoming_id": incoming_id, "canonical_id": canonical.id, "confirm": False},
        headers=_headers(),
    )
    assert resp.status_code == 200

    session.expire_all()
    row = session.get(models.Transaction, incoming_id)
    assert row is not None
    assert row.needs_review is False
    assert row.status == "pending"


def test_confirm_merge_endpoint_merge(client_and_session):
    client, session = client_and_session
    canonical = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    weak = ingest(session, "email", _sms_date_drift())
    session.commit()

    resp = client.post(
        "/webhook/confirm-merge",
        json={"incoming_id": weak.transaction.id, "canonical_id": canonical.id, "confirm": True},
        headers=_headers(),
    )
    assert resp.status_code == 200
    session.expunge(weak.transaction)
    session.expire_all()
    assert session.get(models.Transaction, weak.transaction.id) is None


# ---------- B. refund reduces receivable ----------

def test_refund_reduces_group_expense_receivable(session):
    txn = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    ge = create_group_expense(session, txn, "sam", share_paise=40000)  # receivable 50000 paise
    session.commit()

    credit = ingest(
        session,
        "sms",
        "Rs.500.00 credited from A/c **1234 on 04-08-26 by UPI: swiggy. Ref: 900000000001",
    ).transaction
    session.commit()

    link_refund(session, credit, txn)
    session.commit()

    assert ge.received_so_far == 50000
    assert ge.status == "settled"
    refund_settlement = session.execute(
        select(models.Settlement).where(models.Settlement.kind == "refund")
    ).scalars().first()
    assert refund_settlement is not None
    assert refund_settlement.group_expense_id == ge.id
    assert credit.txn_state == "resolved_shared"


# ---------- C. recurring auto-link ----------

def test_recurring_auto_linked_on_ingest(session):
    rec = models.Recurring(
        pattern="monthly",
        expected_amount=90000,
        merchant="swiggy",
        day_of_month=3,
        category="subscriptions",
        active=True,
    )
    session.add(rec)
    session.commit()

    r = ingest(session, "sms", _sms_swiggy())
    session.commit()

    assert r.outcome == "NEW"
    session.refresh(rec)
    assert rec.last_seen_at is not None


def test_recurring_amount_mismatch_not_linked(session):
    rec = models.Recurring(
        pattern="monthly",
        expected_amount=100000,  # Rs 1000, not 900
        merchant="swiggy",
        day_of_month=3,
        active=True,
    )
    session.add(rec)
    session.commit()

    ingest(session, "sms", _sms_swiggy())
    session.commit()

    session.refresh(rec)
    assert rec.last_seen_at is None


# ---------- D. known-persons learning ----------

def test_learn_person_upsert_and_alias(session):
    learn_person(session, "Sam", alias="sam@okaxis")
    session.commit()
    learn_person(session, "sam", alias="sam2@okaxis")
    session.commit()

    rows = session.execute(select(models.Person)).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "sam"
    assert "sam@okaxis" in rows[0].aliases
    assert "sam2@okaxis" in rows[0].aliases


def test_group_expense_learns_person(session):
    txn = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    create_group_expense(session, txn, "sam")
    session.commit()
    rows = session.execute(select(models.Person)).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "sam"
    assert "swiggy" in rows[0].aliases


# ---------- E. transfer detection ----------

def test_is_transfer_patterns():
    assert is_transfer("wallet topup")
    assert is_transfer("credit card bill payment")
    assert is_transfer("self transfer")
    assert is_transfer("own account")
    assert not is_transfer("swiggy")
    assert not is_transfer("")


def test_transfer_ingest_not_spend(session):
    r = ingest(
        session,
        "sms",
        "Rs.1000.00 debited from A/c **1234 on 03-08-26 to own account via UPI. Ref: 999000000001",
    )
    session.commit()
    assert r.outcome == "NEW"
    assert r.transaction.category == "transfer"

    from app.reports import monthly_digest

    digest = monthly_digest(session, 2026, 8)
    assert digest["spend_paise"] == 0
    assert digest["txn_count"] == 0


# ---------- F. audit log ----------

def test_shared_prompt_writes_audit(client_and_session):
    client, session = client_and_session
    txn = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()

    resp = client.post(
        "/webhook/shared-prompt",
        json={"transaction_id": txn.id, "shared": True},
        headers=_headers(),
    )
    assert resp.status_code == 200

    row = session.execute(select(models.AuditLog)).scalars().first()
    assert row is not None
    assert row.action == "shared_prompt"
    assert row.entity_id == txn.id


def test_settle_writes_audit(client_and_session):
    client, session = client_and_session
    txn = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    ge = create_group_expense(session, txn, "sam", share_paise=400)
    session.commit()

    incoming = ingest(
        session,
        "sms",
        "Rs.500.00 credited from A/c **1234 on 04-08-26 by UPI: sam@okaxis. Ref: 900000000001",
    ).transaction
    session.commit()

    resp = client.post(
        "/webhook/settle",
        json={"transaction_id": incoming.id, "group_expense_id": ge.id},
        headers=_headers(),
    )
    assert resp.status_code == 200

    row = session.execute(select(models.AuditLog)).scalars().first()
    assert row is not None
    assert row.action == "settle"
