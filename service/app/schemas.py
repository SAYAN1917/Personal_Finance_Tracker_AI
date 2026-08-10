"""Pydantic schemas for the API."""

from datetime import datetime

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    source: str = Field(..., description="sms/email/card_pdf/upi_csv/telegram")
    text: str = Field(..., min_length=1)
    channel: str | None = None


class SharedPromptRequest(BaseModel):
    transaction_id: int
    shared: bool


class GroupExpenseRequest(BaseModel):
    transaction_id: int
    person: str
    share_amount_paise: int | None = None
    """Your share in paise. Receivable = full_amount - share (Section 7.3)."""


class SettleRequest(BaseModel):
    transaction_id: int
    group_expense_id: int
    amount_paise: int | None = None
    """Amount to apply against the receivable, in paise. Defaults to txn amount."""


class ReconcileRequest(BaseModel):
    account: str
    statement_balance_paise: int
    """Current balance shown by the bank. Positive = money at hand."""
    as_of: datetime | None = None


class RecurringRequest(BaseModel):
    pattern: str = "monthly"
    expected_amount_paise: int
    category: str | None = None
    merchant: str = ""
    day_of_month: int | None = None


class ConfirmMergeRequest(BaseModel):
    incoming_id: int
    canonical_id: int
    confirm: bool


class TransactionOut(BaseModel):
    id: int
    txn_date: datetime
    amount_paise: int
    type: str
    mode: str
    counterparty_norm: str
    status: str
    txn_state: str
    credit_type: str | None
    category: str | None
    sources: str

    model_config = {"from_attributes": True}
