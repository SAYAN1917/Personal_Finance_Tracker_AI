"""Tests for production hardening:

- fail-closed secrets (settings.validate)
- fail-closed bot allowlist
- /ready migrations check
- rate limiting on webhooks
"""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.bot import TelegramBot
from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/api.db"
    settings.webhook_secret = "test-secret"
    settings.rate_limit_per_min = 0
    db.reset_db()
    db.init_db()
    with TestClient(app) as c:
        yield c


def _headers():
    return {"X-Webhook-Secret": "test-secret"}


def _sms(ref="412345678999"):
    return f"Rs.900.00 debited from A/c **1234 on 03-08-26 by UPI: swiggy. Ref: {ref}"


# ---------- fail-closed secrets ----------

def test_dev_validate_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "environment", "dev")
    monkeypatch.setattr(settings, "webhook_secret", "")
    settings.validate(require_bot=True)  # must not raise in dev


def test_prod_validate_fails_on_placeholder_secret(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "webhook_secret", "dev-secret-change-me")
    monkeypatch.setattr(settings, "admin_user_id", "123")
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        settings.validate()


def test_prod_validate_fails_on_empty_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "webhook_secret", "real-secret")
    monkeypatch.setattr(settings, "admin_user_id", "")
    with pytest.raises(RuntimeError, match="ADMIN_USER_ID"):
        settings.validate()


def test_prod_validate_requires_bot_token(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "webhook_secret", "real-secret")
    monkeypatch.setattr(settings, "admin_user_id", "123")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        settings.validate(require_bot=True)


def test_prod_validate_passes_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "webhook_secret", "real-secret")
    monkeypatch.setattr(settings, "admin_user_id", "123")
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    settings.validate(require_bot=True)  # no raise


# ---------- fail-closed bot allowlist ----------

def test_bot_rejects_everyone_without_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "admin_user_id", "")
    bot = TelegramBot(token="test")
    assert bot._is_allowed(123) is False
    assert bot._is_allowed(0) is False


def test_bot_allowlist_enforces(monkeypatch):
    monkeypatch.setattr(settings, "admin_user_id", "123,456")
    bot = TelegramBot(token="test")
    assert bot._is_allowed(123) is True
    assert bot._is_allowed(456) is True
    assert bot._is_allowed(789) is False


# ---------- /ready ----------

def test_ready_reports_up_to_date(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["migrations"] == "up_to_date"
    assert body["status"] == "ready"


# ---------- rate limiting ----------

def test_rate_limit_429_after_budget(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_min", 2)
    try:
        r1 = client.post("/webhook/ingest", json={"source": "sms", "text": _sms()}, headers=_headers())
        assert r1.status_code == 200
        r2 = client.post("/webhook/ingest", json={"source": "sms", "text": _sms(ref="2")}, headers=_headers())
        assert r2.status_code == 200
        r3 = client.post("/webhook/ingest", json={"source": "sms", "text": _sms(ref="3")}, headers=_headers())
        assert r3.status_code == 429
    finally:
        monkeypatch.setattr(settings, "rate_limit_per_min", 0)


def test_rate_limit_disabled_by_default(client):
    # RATE_LIMIT_PER_MIN=0 => unlimited
    for i in range(5):
        resp = client.post(
            "/webhook/ingest",
            json={"source": "sms", "text": _sms(ref=str(i))},
            headers=_headers(),
        )
        assert resp.status_code == 200
