"""Scheduled digests (FINAL_PLAN.md Phase 6): push a summary over Telegram.

Runs as a cron/systemd-timer job via the `finance-digest` entry point; nothing
runs in-process. Periods: daily | weekly | monthly.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

from sqlalchemy import select

from app import db, models
from app.bot import TelegramBot
from app.config import settings
from app.logging import setup_logging
from app.reports import due_recurring, monthly_digest

logger = logging.getLogger(__name__)


def _digest_chat_id() -> int | None:
    if settings.digest_chat_id:
        return int(settings.digest_chat_id)
    first = settings.admin_user_id.split(",")[0].strip() if settings.admin_user_id else ""
    return int(first) if first else None


def build_digest(session, period: str = "daily") -> str:
    today = date.today()
    digest = monthly_digest(session, today.year, today.month)
    lines = [
        f"Finance digest ({period}) - {today:%d %b %Y}",
        "",
        f"Spend this month: Rs {digest['spend_paise'] / 100:.0f}",
        f"Income: Rs {digest['income_paise'] / 100:.0f}",
        f"Net: Rs {digest['net_paise'] / 100:.0f}",
        f"Transactions: {digest['txn_count']}",
    ]
    if digest["top_category"]:
        lines.append(f"Top category: {digest['top_category']['category']} "
                     f"(Rs {digest['top_category']['spend_paise'] / 100:.0f})")

    # Balances (who owes you)
    ge_rows = session.execute(select(models.GroupExpense)).scalars().all()
    open_ge = [ge for ge in ge_rows if ge.expected_receivable > ge.received_so_far]
    if open_ge:
        lines.append("")
        lines.append("Receivables:")
        for ge in open_ge:
            net = ge.expected_receivable - ge.received_so_far
            lines.append(f"- {ge.person}: Rs {net / 100:.0f}")

    # Recurring bills due
    if period in ("weekly", "monthly"):
        due = due_recurring(session)
        if due:
            lines.append("")
            lines.append("Bills due soon:")
            for rec in due:
                lines.append(f"- {rec.merchant or rec.category}: Rs {rec.expected_amount / 100:.0f}")

    # Review queue
    needs_review = session.execute(
        select(models.Transaction).where(models.Transaction.needs_review == True)  # noqa: E712
    ).scalars().all()
    if needs_review:
        lines.append("")
        lines.append(f"Review queue: {len(needs_review)} item(s) pending - use /find to inspect.")

    return "\n".join(lines)


def run() -> None:
    """CLI entry point: build + send one digest, then exit."""
    setup_logging("digest")
    settings.validate(require_bot=True)
    period = sys.argv[1] if len(sys.argv) > 1 else settings.digest_period
    chat_id = _digest_chat_id()
    if chat_id is None:
        logger.error("Digest chat id unavailable - set DIGEST_CHAT_ID or ADMIN_USER_ID.")
        return
    with db.session_scope() as session:
        text = build_digest(session, period)
    bot = TelegramBot(settings.telegram_bot_token)
    bot.send_message(chat_id, text)
    logger.info("Digest (%s) sent to %s", period, chat_id)


if __name__ == "__main__":
    run()
