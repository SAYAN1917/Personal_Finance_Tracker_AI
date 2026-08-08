"""SQLAlchemy models - the canonical schema (FINAL_PLAN.md Section 10).

All aggregates are derived, never stored totals, so late entries recompute
idempotently (event-sourcing discipline).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Event(Base):
    """Immutable raw event log - every message, even duplicates (Section 3.1)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)


class Account(Base):
    """Per-account balance source of truth; reconciliation target."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="bank")  # bank/wallet/card/cash
    institution: Mapped[str] = mapped_column(String(64), default="")
    last_verified_balance_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Transaction(Base):
    """Canonical ledger row. Duplicates merge into this; never re-inserted."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_txn_key_exact", "key_exact", unique=True),
        Index("ix_txn_counterparty_date", "counterparty_norm", "txn_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    txn_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posting_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)  # signed
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    fx_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    type: Mapped[str] = mapped_column(String(8), nullable=False)  # debit/credit
    mode: Mapped[str] = mapped_column(String(16), default="UPI")  # UPI/CARD/NEFT/IMPS/cash
    counterparty_norm: Mapped[str] = mapped_column(String(128), default="")
    account: Mapped[str] = mapped_column(String(32), default="")

    utr: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    key_exact: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    key_soft: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)

    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/confirmed/verified
    lifecycle: Mapped[str] = mapped_column(String(16), default="active")  # active/failed/reversed
    txn_state: Mapped[str] = mapped_column(String(24), default="personal")
    # personal/maybe_shared/flagged_shared/resolved_shared
    credit_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # refund/reimbursement/income/null
    ownership: Mapped[str] = mapped_column(String(8), default="mine")  # mine/not_mine
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)

    bill_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    emi_group: Mapped[str | None] = mapped_column(String(32), nullable=True)

    sources: Mapped[str] = mapped_column(String(128), default="[]")  # JSON list of channel aliases
    parser_version: Mapped[int] = mapped_column(Integer, default=1)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)

    group_expenses: Mapped[list["GroupExpense"]] = relationship(back_populates="transaction")


class DedupMap(Base):
    """Every merge/alias - provenance for the confidence ladder."""

    __tablename__ = "dedup_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_id: Mapped[str] = mapped_column(String(128), nullable=False)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefundLink(Base):
    """Refund credit linked back to its original expense."""

    __tablename__ = "refund_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    original_txn_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="auto")  # auto/needs_review
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GroupExpense(Base):
    """The receivable - only created via explicit 'Create group expense'."""

    __tablename__ = "group_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    person: Mapped[str] = mapped_column(String(64), nullable=False)
    full_amount: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    share_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)  # your share, paise
    expected_receivable: Mapped[int] = mapped_column(Integer, nullable=False)
    received_so_far: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open/partial/settled/written_off
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    transaction: Mapped["Transaction"] = relationship(back_populates="group_expenses")
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="group_expense")


class Settlement(Base):
    """A matched inbound credit closing part or all of a group expense."""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    group_expense_id: Mapped[int] = mapped_column(ForeignKey("group_expenses.id"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="partial")  # partial/full/overpay/cash
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    group_expense: Mapped["GroupExpense"] = relationship(back_populates="settlements")


class Person(Base):
    """Known-persons list - checked before any category regex (Section 6.1)."""

    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    vpas: Mapped[str] = mapped_column(String(256), default="[]")  # JSON list
    aliases: Mapped[str] = mapped_column(String(256), default="[]")  # JSON list


class PendingAction(Base):
    """Multi-step bot conversations (create group expense, settle picker, etc.)."""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditLog(Base):
    """Every manual edit/merge/delete - who, what, when, why."""

    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(32), default="system")
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(128), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Recurring(Base):
    """Known subscriptions/bills - pre-flag + reminders."""

    __tablename__ = "recurring"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern: Mapped[str] = mapped_column(String(16), default="monthly")  # monthly/quarterly
    expected_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merchant: Mapped[str] = mapped_column(String(128), default="")
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
