"""FastAPI application - webhook ingestion + ledger + bot-facing endpoints."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import db, models
from app.audit import audit
from app.config import settings
from app.ingest import ingest
from app.logging import setup_logging
from app.ratelimit import enforce_rate_limit
from app.schemas import (
    ConfirmMergeRequest,
    GroupExpenseRequest,
    IngestRequest,
    ReconcileRequest,
    RecurringRequest,
    SettleRequest,
    SharedPromptRequest,
    TransactionOut,
)
from app.settlements import apply_settlement

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("api")
    settings.validate()  # fail fast on missing prod secrets
    db.init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Finance Tracker Core", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_session():
    Session = db.get_sessionmaker()
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _check_webhook_secret(authorization: str | None, x_webhook_secret: str | None):
    """Shared-secret auth for N8N -> Core webhooks (Section 13)."""
    presented = (authorization or "").removeprefix("Bearer ").strip() or (x_webhook_secret or "").strip()
    if not presented or presented != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health():
    """Cheap liveness for UptimeRobot - does NOT do a full reconcile."""
    try:
        with db.session_scope() as session:
            session.execute(select(1))
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check failed: %s", exc)
        raise HTTPException(status_code=503, detail="db_down")


@app.get("/ready")
def ready():
    """Readiness: DB reachable + migrations up to date. Heavy checks live here,
    not in /health."""
    try:
        from app.db import check_migrations

        with db.session_scope() as session:
            session.execute(select(1))
            migration_state = check_migrations(session)
        if migration_state != "up_to_date":
            raise HTTPException(status_code=503, detail="migrations_pending")
        return {"status": "ready", "migrations": migration_state, "env": settings.environment}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Ready check failed: %s", exc)
        raise HTTPException(status_code=503, detail="not_ready")


@app.post("/webhook/ingest")
def webhook_ingest(
    req: IngestRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    _rate_limit: None = Depends(enforce_rate_limit),
):
    _check_webhook_secret(authorization, x_webhook_secret)
    result = ingest(session, req.source, req.text, req.channel or req.source)
    session.commit()

    payload = {
        "outcome": result.outcome,
        "message": result.message,
        "transaction_id": result.transaction.id if result.transaction else None,
        "needs_review": result.needs_review,
        "settlement_candidate": (
            {"id": result.settlement_candidate.id, "person": result.settlement_candidate.person}
            if result.settlement_candidate
            else None
        ),
    }
    return payload


@app.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    query = select(models.Transaction).order_by(models.Transaction.txn_date.desc()).limit(limit)
    if status:
        query = query.where(models.Transaction.status == status)
    return session.execute(query).scalars().all()


@app.get("/ledger")
def ledger(
    session: Session = Depends(get_session),
    include_flagged: bool = Query(default=True),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    query = select(models.Transaction).order_by(models.Transaction.id)
    if not include_flagged:
        query = query.where(models.Transaction.txn_state != "flagged_shared")
    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar_one()
    rows = session.execute(query.offset(offset).limit(limit) if limit else query.offset(offset)).scalars().all()
    data = []
    for t in rows:
        data.append(
            {
                "id": t.id,
                "amount_paise": t.amount_paise,
                "type": t.type,
                "status": t.status,
                "txn_state": t.txn_state,
                "category": t.category,
                "counterparty": t.counterparty_norm,
                "utr": t.utr,
                "sources": t.sources,
                "credit_type": t.credit_type,
            }
        )
    return {"total": total, "offset": offset, "limit": limit, "items": data}


@app.post("/webhook/shared-prompt")
def shared_prompt(
    req: SharedPromptRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    _rate_limit: None = Depends(enforce_rate_limit),
):
    """Answer to 'is this shared?' - sets flagged_shared or confirms personal."""
    _check_webhook_secret(authorization, x_webhook_secret)
    txn = session.get(models.Transaction, req.transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if req.shared:
        before = txn.txn_state
        txn.txn_state = "flagged_shared"
        txn.needs_review = False
        audit(session, "api", "shared_prompt", "transaction", txn.id, before=before, after="flagged_shared")
        message = "Flagged shared - full amount stays in spend. Create group expense to make it a receivable."
    else:
        before = txn.txn_state
        txn.txn_state = "personal"
        txn.needs_review = False
        audit(session, "api", "shared_prompt", "transaction", txn.id, before=before, after="personal")
        message = "Marked personal."
    session.commit()
    return {"message": message, "transaction_id": txn.id, "txn_state": txn.txn_state}


@app.post("/webhook/confirm-merge")
def confirm_merge(
    req: ConfirmMergeRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    _rate_limit: None = Depends(enforce_rate_limit),
):
    """Human resolution for a WEAK dedup txn: [Merge] folds it into the
    canonical row, [No] keeps it as a distinct transaction."""
    _check_webhook_secret(authorization, x_webhook_secret)
    incoming = session.get(models.Transaction, req.incoming_id)
    canonical = session.get(models.Transaction, req.canonical_id)
    if not incoming or not canonical:
        raise HTTPException(status_code=404, detail="Incoming or canonical transaction not found")

    from app.dedup import merge_transaction

    if req.confirm:
        merge_transaction(session, incoming, canonical)
        audit(session, "api", "merge", "transaction", incoming.id, before="review", after=f"merged into #{canonical.id}")
        session.commit()
        return {"message": "Merged into canonical", "canonical_id": canonical.id}
    incoming.needs_review = False
    incoming.status = "pending"
    audit(session, "api", "merge_no", "transaction", incoming.id, before="needs_review", after="kept distinct")
    session.commit()
    return {"message": "Kept as a distinct transaction", "incoming_id": incoming.id}


@app.post("/webhook/group-expense")
def create_group_expense(
    req: GroupExpenseRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    _rate_limit: None = Depends(enforce_rate_limit),
):
    """Create the receivable (flag != receivable; this is what makes it real)."""
    _check_webhook_secret(authorization, x_webhook_secret)
    txn = session.get(models.Transaction, req.transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    from app.settlements import create_group_expense as make_group_expense

    ge = make_group_expense(session, txn, req.person, req.share_amount_paise)
    audit(
        session,
        "api",
        "group_expense",
        "group_expense",
        ge.id,
        before=f"txn #{txn.id} flagged_shared",
        after=f"receivable {ge.expected_receivable}",
        reason=f"person={ge.person}",
    )
    session.commit()
    return {
        "group_expense_id": ge.id,
        "person": ge.person,
        "expected_receivable": ge.expected_receivable,
        "note": "Share unknown - will be computed at settlement"
        if ge.share_amount is None
        else "Receivable tracked",
    }


@app.post("/webhook/settle")
def settle(
    req: SettleRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    _rate_limit: None = Depends(enforce_rate_limit),
):
    _check_webhook_secret(authorization, x_webhook_secret)
    txn = session.get(models.Transaction, req.transaction_id)
    ge = session.get(models.GroupExpense, req.group_expense_id)
    if not txn or not ge:
        raise HTTPException(status_code=404, detail="Transaction or group expense not found")

    state = apply_settlement(session, txn, ge, req.amount_paise)
    audit(
        session,
        "api",
        "settle",
        "settlement",
        txn.id,
        before=f"txn #{txn.id} pending",
        after=f"applied {state['applied']}",
        reason=f"ge #{ge.id}",
    )
    session.commit()
    return {"message": "Settled", "state": state}


@app.get("/group-expenses")
def list_group_expenses(session: Session = Depends(get_session)):
    rows = session.execute(select(models.GroupExpense)).scalars().all()
    return [
        {
            "id": ge.id,
            "person": ge.person,
            "full_amount": ge.full_amount,
            "share_amount": ge.share_amount,
            "expected_receivable": ge.expected_receivable,
            "received_so_far": ge.received_so_far,
            "status": ge.status,
            "transaction_id": ge.transaction_id,
        }
        for ge in rows
    ]


@app.get("/balances")
def balances(session: Session = Depends(get_session)):
    """Net receivable per person (derived, recomputed)."""
    rows = session.execute(select(models.GroupExpense)).scalars().all()
    by_person: dict[str, dict] = {}
    for ge in rows:
        net = ge.expected_receivable - ge.received_so_far
        entry = by_person.setdefault(
            ge.person, {"expected": 0, "received": 0, "net": 0}
        )
        entry["expected"] += ge.expected_receivable
        entry["received"] += ge.received_so_far
        entry["net"] += net
    return by_person


@app.post("/reconcile")
def reconcile(
    req: ReconcileRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
):
    """Reconcile an account against a statement balance (Section 10.3)."""
    _check_webhook_secret(authorization, x_webhook_secret)
    from app.reconcile import reconcile_account

    state = reconcile_account(session, req.account, req.statement_balance_paise, req.as_of)
    session.commit()
    return state


@app.get("/confidence")
def confidence(session: Session = Depends(get_session)):
    """Confidence ladder summary: pending/confirmed/verified counts."""
    from app.reconcile import confidence_report

    return confidence_report(session)


@app.get("/refunds")
def list_refunds(session: Session = Depends(get_session)):
    """All refund links."""
    rows = session.execute(select(models.RefundLink)).scalars().all()
    return [
        {
            "id": link.id,
            "txn_id": link.txn_id,
            "original_txn_id": link.original_txn_id,
            "amount": link.amount,
            "confidence": link.confidence,
            "matched_at": link.matched_at,
        }
        for link in rows
    ]


@app.get("/report/monthly")
def monthly_report(
    year: int | None = None,
    month: int | None = None,
    session: Session = Depends(get_session),
):
    """Monthly digest + category breakdown."""
    from app.reports import monthly_digest

    now = datetime.now()
    return monthly_digest(
        session,
        year or now.year,
        month or now.month,
    )


@app.get("/recurring/due")
def recurring_due(session: Session = Depends(get_session)):
    """Recurring bills due within the next 3 days."""
    from app.reports import due_recurring

    rows = due_recurring(session)
    return [
        {
            "id": rec.id,
            "merchant": rec.merchant,
            "expected_amount": rec.expected_amount,
            "category": rec.category,
            "day_of_month": rec.day_of_month,
            "last_seen_at": rec.last_seen_at,
        }
        for rec in rows
    ]


@app.post("/recurring")
def create_recurring(
    req: RecurringRequest,
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    _rate_limit: None = Depends(enforce_rate_limit),
):
    """Register a known recurring bill for reminders."""
    _check_webhook_secret(authorization, x_webhook_secret)
    rec = models.Recurring(
        pattern=req.pattern,
        expected_amount=req.expected_amount_paise,
        category=req.category,
        merchant=req.merchant,
        day_of_month=req.day_of_month,
        active=True,
    )
    session.add(rec)
    audit(
        session,
        "api",
        "recurring",
        "recurring",
        rec.id,
        after=f"amount {rec.expected_amount} merchant={rec.merchant}",
    )
    session.commit()
    return {"id": rec.id, "merchant": rec.merchant}
