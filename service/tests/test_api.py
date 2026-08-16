"""API tests via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/api.db"
    settings.webhook_secret = "test-secret"
    from app import db

    db.reset_db()
    db.init_db()
    with TestClient(app) as c:
        yield c


def _headers():
    return {"X-Webhook-Secret": "test-secret"}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_ready(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["migrations"] == "up_to_date"


def test_ingest_requires_auth(client):
    resp = client.post(
        "/webhook/ingest",
        json={"source": "sms", "text": "Rs.100.00 debited"},
    )
    assert resp.status_code == 401


def test_ingest_and_ledger(client):
    resp = client.post(
        "/webhook/ingest",
        json={
            "source": "sms",
            "text": "Rs.900.00 debited from A/c **1234 on 03-08-26 by UPI: swiggy. Ref: 412345678999",
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "NEW"

    ledger = client.get("/ledger").json()
    assert ledger["total"] == 1
    assert ledger["items"][0]["counterparty"] == "swiggy"


def test_group_expense_and_balance(client):
    r = client.post(
        "/webhook/ingest",
        json={"source": "telegram", "text": "dinner 900"},
        headers=_headers(),
    )
    txn_id = r.json()["transaction_id"]

    ge = client.post(
        "/webhook/group-expense",
        json={"transaction_id": txn_id, "person": "sam", "share_amount_paise": 30000},
        headers=_headers(),
    )
    assert ge.status_code == 200
    assert ge.json()["expected_receivable"] == 60000

    balances = client.get("/balances").json()
    assert balances["sam"]["net"] == 60000


def test_shared_prompt_flag(client):
    r = client.post(
        "/webhook/ingest",
        json={"source": "telegram", "text": "dinner 900"},
        headers=_headers(),
    )
    txn_id = r.json()["transaction_id"]
    resp = client.post(
        "/webhook/shared-prompt",
        json={"transaction_id": txn_id, "shared": True},
        headers=_headers(),
    )
    assert resp.json()["txn_state"] == "flagged_shared"
