"""Tests for the full ingest pipeline + dedup engine (Prototype bugs A-F)."""

import pytest

from app import db, models
from app.config import settings
from app.ingest import ingest


@pytest.fixture()
def session(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/test.db"
    db.reset_db()
    db.init_db()
    Session = db.get_sessionmaker()
    s = Session()
    yield s
    s.close()


def _sms_swiggy():
    return {
        "source": "sms",
        "text": "Rs.900.00 debited from A/c **1234 on 03-08-26 by UPI: swiggy. Ref: 412345678999",
    }


def test_utr_dedup_across_sources(session):
    """SMS first, same txn via email -> duplicate (Tier 1 UTR)."""
    r1 = ingest(session, "sms", _sms_swiggy()["text"])
    session.commit()
    assert r1.outcome == "NEW"
    assert r1.transaction.status == "pending"

    r2 = ingest(session, "email", _sms_swiggy()["text"])
    session.commit()
    assert r2.outcome == "EXACT"
    assert r2.transaction.id == r1.transaction.id
    assert r2.transaction.status == "confirmed"  # 2 sources
    assert "sms" in r2.transaction.sources and "email" in r2.transaction.sources


def test_same_source_resend_is_duplicate_not_crash(session):
    """Prototype Bug A: SMS Forwarder re-send must dedup, not IntegrityError."""
    ingest(session, "sms", _sms_swiggy()["text"])
    session.commit()
    # same source, same UTR again
    r = ingest(session, "sms", _sms_swiggy()["text"])
    session.commit()
    assert r.outcome == "EXACT"


def test_pdf_fingerprint_merge(session):
    """Card PDF without UTR -> Tier 2 fingerprint merge."""
    r1 = ingest(session, "sms", _sms_swiggy()["text"])
    session.commit()

    r2 = ingest(
        session,
        "card_pdf",
        "Rs.900.00 debited on 03-08-26 swiggy",
    )
    session.commit()
    assert r2.outcome in ("EXACT", "STRONG")
    assert r2.transaction.id == r1.transaction.id


def test_credit_never_matches_debit(session):
    """Prototype Bug C: a Rs 900 credit must not merge into the Rs 900 debit."""
    ingest(session, "sms", _sms_swiggy()["text"])
    session.commit()
    r = ingest(
        session,
        "sms",
        "Rs.900.00 credited to A/c **1234 on 03-08-26 by UPI: ravi@ybl. Ref: 412345678998",
    )
    session.commit()
    assert r.outcome == "NEW"
    assert r.transaction.type == "credit"


def test_failed_parse_goes_to_review(session):
    r = ingest(session, "sms", "hello there no numbers here")
    session.commit()
    assert r.outcome == "FAILED"
    assert r.needs_review is True


def test_event_log_records_everything(session):
    ingest(session, "sms", _sms_swiggy()["text"])
    session.commit()
    count = session.query(models.Event).count()
    assert count == 1
    ev = session.query(models.Event).first()
    assert ev.raw_payload == _sms_swiggy()["text"]
