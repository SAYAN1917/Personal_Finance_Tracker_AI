"""Tests for the Telegram bot flow (Phase 3).

Uses a stub bot that records outgoing messages instead of calling Telegram.
"""

from __future__ import annotations

import pytest

from app import db, models
from app.bot import TelegramBot
from app.config import settings
from app.ingest import ingest
from app.settlements import create_group_expense


class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="test-token")
        self.sent: list[dict] = []
        self.answered: list[str] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    def answer_callback(self, callback_id, text=""):
        self.answered.append(callback_id)


@pytest.fixture
def fake_bot(tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/bot.db"
    db.reset_db()
    db.init_db()
    return FakeBot()


def _last_text(bot: FakeBot) -> str:
    return bot.sent[-1]["text"]


def test_shared_prompt_triggered_on_odd_amount(fake_bot):
    bot = fake_bot
    bot._ingest_text(123, "dinner 451.25")
    # odd amounts trigger the shared prompt
    last = bot.sent[-1]
    assert "shared expense?" in last["text"].lower()
    assert last["reply_markup"]["inline_keyboard"]


def test_mkgroup_creates_receivable(fake_bot):
    bot = fake_bot
    with db.session_scope() as s:
        res = ingest(s, "telegram", "dinner 900")
        txn = res.transaction
        txn_id = txn.id
    bot._on_shared_answer(123, txn_id, shared=True)
    bot._on_mkgroup(123, txn_id)  # user taps "Create group expense"
    # now continues with 'group sam 300'
    bot._handle_pending_continuation(123, "group sam 300")
    assert "sam" in _last_text(bot)
    with db.session_scope() as s:
        ge = s.query(models.GroupExpense).first()
        assert ge is not None
        assert ge.person == "sam"
        assert ge.full_amount == 90000
        assert ge.share_amount == 30000
        assert ge.expected_receivable == 60000
        assert ge.status == "open"


def test_mkgroup_then_settle(fake_bot):
    with db.session_scope() as s:
        res = ingest(s, "telegram", "dinner 900")
        txn = res.transaction
        ge = create_group_expense(s, txn, "sam", 30000)
        ge_id = ge.id
    # inbound credit of 60000 from sam should be suggested as settlement
    with db.session_scope() as s:
        res2 = ingest(s, "telegram", "Rs.60000.00 credited from sam Ref: 1111111111111111")
        assert res2.settlement_candidate is not None
        assert res2.settlement_candidate.id == ge_id


def test_allowlist_blocks_unknown_user(fake_bot, monkeypatch):
    monkeypatch.setattr("app.config.settings.admin_user_id", "999")
    bot = fake_bot
    message = {"chat": {"id": 1}, "from": {"id": 2}, "text": "dinner 100"}
    bot._handle_message(message)
    assert bot.sent == []


def test_settlement_confirmed_marks_reimbursement(fake_bot):
    bot = fake_bot
    with db.session_scope() as s:
        res = ingest(s, "telegram", "dinner 900")
        txn = res.transaction
        ge = create_group_expense(s, txn, "sam", 30000)
        ge_id = ge.id
        txn.status = "confirmed"
        s.add(txn)
    with db.session_scope() as s:
        res2 = ingest(s, "telegram", "Rs.60000.00 credited from sam Ref: 2222222222222222")
        incoming_id = res2.transaction.id
    bot._on_settle(123, incoming_id, ge_id)
    with db.session_scope() as s:
        ge = s.get(models.GroupExpense, ge_id)
        txn_in = s.get(models.Transaction, incoming_id)
        assert ge.status == "settled"
        assert ge.received_so_far == 60000
        assert txn_in.credit_type == "reimbursement"
        assert txn_in.txn_state == "resolved_shared"


def test_quiet_hours_suppress_prompt(fake_bot, monkeypatch):

    bot = fake_bot
    monkeypatch.setattr(
        "app.config.settings.quiet_hours_start", 23
    )
    monkeypatch.setattr(
        "app.config.settings.quiet_hours_end", 7
    )
    monkeypatch.setattr(
        bot.__class__, "_in_quiet_hours", lambda self: True
    )
    bot._ingest_text(123, "dinner 451.25")
    last = bot.sent[-1]
    assert "recorded" in last["text"].lower()
    assert "shared expense?" not in last["text"].lower()


def test_mkgroup_share_optional(fake_bot):
    """Bug: 'group sam' (no share) failed silently and dropped the pending
    action. Share must be optional -> deferred receivable."""
    bot = fake_bot
    with db.session_scope() as s:
        res = ingest(s, "telegram", "dinner 900")
        txn_id = res.transaction.id
    bot._on_shared_answer(123, txn_id, shared=True)
    bot._on_mkgroup(123, txn_id)
    bot._handle_pending_continuation(123, "group sam")
    assert "sam" in _last_text(bot)
    with db.session_scope() as s:
        ge = s.query(models.GroupExpense).first()
        assert ge is not None
        assert ge.person == "sam"
        assert ge.share_amount is None
        assert ge.expected_receivable == 0


def test_mkgroup_bad_input_keeps_pending(fake_bot):
    bot = fake_bot
    with db.session_scope() as s:
        res = ingest(s, "telegram", "dinner 900")
        txn_id = res.transaction.id
    bot._on_shared_answer(123, txn_id, shared=True)
    bot._on_mkgroup(123, txn_id)
    bot._handle_pending_continuation(123, "9999")
    # pending action survives so the user can retry
    with db.session_scope() as s:
        assert s.query(models.PendingAction).count() == 1
        assert s.query(models.GroupExpense).count() == 0


def test_credit_from_person_parses_for_settlement(fake_bot):
    from app.parser import parse_telegram_entry

    p = parse_telegram_entry("Rs.5000.00 credited from priya Ref: 3333333333333333")
    assert p.txn_type == "credit"
    assert p.amount_paise == 500000
    assert p.counterparty == "priya"
