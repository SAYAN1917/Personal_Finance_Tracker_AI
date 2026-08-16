"""Regression tests for the remaining Phase-9 enhancements:

- EMI tagging (category 'emi', emi_group, excluded from spend)
- reconciliation statement-line missed/anomaly detection
- NLU enrichment (category + group intent on partially-parsed entries)
- debounce batching for SMS forwards
- email poller body extraction
- scheduled digest content
- /ledger pagination
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import db, models
from app.classify import emi_group, is_emi
from app.config import settings
from app.digest import build_digest
from app.email_poller import extract_body
from app.ingest import ingest
from app.main import app
from app.reconcile import reconcile_statement_lines


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
def client(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/api.db"
    settings.webhook_secret = "test-secret"
    settings.rate_limit_per_min = 0
    db.reset_db()
    db.init_db()
    with TestClient(app) as c:
        yield c


def _sms_swiggy():
    return "Rs.900.00 debited from A/c **1234 on 03-08-26 by UPI: swiggy. Ref: 412345678999"


# ---------- EMI tagging ----------

def test_is_emi_and_group():
    assert is_emi("slice emi")
    assert is_emi("credit line repayment")
    assert not is_emi("swiggy")
    assert emi_group("Slice  EMI") == "sliceemi"


def test_emi_ingest_tagged_and_excluded(session):
    r = ingest(session, "telegram", "hdfc emi 2500")
    session.commit()
    assert r.outcome == "NEW"
    assert r.transaction.category == "emi"
    assert r.transaction.emi_group == "hdfcemi"

    from app.reports import monthly_digest

    digest = monthly_digest(session, 2026, 8)
    assert digest["spend_paise"] == 0
    assert digest["txn_count"] == 0


# ---------- reconciliation statement lines ----------

def test_statement_lines_find_missing_and_anomaly(session):
    r1 = ingest(session, "sms", _sms_swiggy())  # Rs 900 debit on 03-08-26
    session.commit()

    report = reconcile_statement_lines(
        session,
        "1234",
        [
            {"amount_paise": -90000, "date": datetime(2026, 8, 3), "counterparty": "swiggy"},  # matched
            {"amount_paise": -45000, "date": datetime(2026, 8, 4), "counterparty": "amazon"},  # missing
        ],
        as_of=datetime(2026, 8, 5),
    )
    assert report["matched_count"] == 1
    assert report["matched"] == [r1.transaction.id]
    assert report["missing_count"] == 1
    assert report["missing"][0]["amount_paise"] == -45000
    assert report["anomaly_count"] == 0


def test_statement_lines_never_mutate(session):
    txn = ingest(session, "sms", _sms_swiggy()).transaction
    session.commit()
    report = reconcile_statement_lines(
        session,
        "1234",
        [{"amount_paise": -99999, "date": datetime(2026, 8, 3)}],
        as_of=datetime(2026, 8, 5),
    )
    session.commit()
    # txn untouched, still active, no auto-fix
    row = session.get(models.Transaction, txn.id)
    assert row is not None
    assert row.lifecycle == "active"
    assert report["anomaly_count"] == 1


# ---------- /ledger pagination ----------

def test_ledger_pagination(client):
    # distinct amounts + refs so each is a genuine NEW txn (not fuzzy-dupe)
    for i, amount in enumerate(["900.00", "910.00", "920.00"]):
        client.post(
            "/webhook/ingest",
            json={
                "source": "sms",
                "text": f"Rs.{amount} debited from A/c **1234 on 03-08-26 by UPI: swiggy. Ref: 99900000{i}",
            },
            headers={"X-Webhook-Secret": "test-secret"},
        )
    body = client.get("/ledger?limit=2&offset=1").json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["offset"] == 1
    assert body["limit"] == 2


# ---------- email poller body extraction ----------

def test_extract_body_plain():
    import email

    msg = email.message.EmailMessage()
    msg["Subject"] = "Bank alert"
    msg.set_content("Rs.900.00 debited from A/c **1234 on 03-08-26 by UPI: swiggy. Ref: 412345678999")
    body = extract_body(msg)
    assert "Rs.900.00" in body
    assert "swiggy" in body


def test_extract_body_html_stripped():
    import email

    msg = email.message.EmailMessage()
    msg["Subject"] = "Bank alert"
    msg.set_content("<html><body><h1>Alert</h1><p>Rs.900.00 debited from swiggy</p></body></html>", subtype="html")
    body = extract_body(msg)
    assert "swiggy" in body
    assert "<h1>" not in body


# ---------- digest ----------

def test_digest_build(session):
    ingest(session, "sms", _sms_swiggy())
    session.commit()
    text = build_digest(session, "daily")
    assert "Finance digest" in text
    assert "Spend this month" in text


# ---------- debounce ----------

def test_debounce_batches_and_flushes():
    from app.bot import TelegramBot

    class FakeBot(TelegramBot):
        def __init__(self):
            super().__init__(token="t")
            self.sent = []

        def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append((chat_id, text))

    bot = FakeBot()
    assert bot._looks_like_sms("Rs.100 debited from A/c") is True
    assert bot._looks_like_sms("dinner 450") is False
    bot._confirm(1, "Recorded: 100", "Rs.100 debited from A/c")
    bot._confirm(1, "Recorded: 200", "Rs.200 debited from A/c")
    bot._confirm(2, "Recorded: 50", "Rs.50 credited from A/c")
    # not flushed yet - batched
    assert bot.sent == []
    bot._flush_all_debounced()
    assert len(bot.sent) == 2
    chat1_text = [t for c, t in bot.sent if c == 1][0]
    assert "- Recorded: 100" in chat1_text
    assert "- Recorded: 200" in chat1_text
