"""Telegram bot - the primary UI (FINAL_PLAN.md Sections 7, 9, 13).

Long-polling (no public webhook), allowlisted user only, inline keyboards for
trigger-driven shared prompts, debounce, quiet hours, settlement confirmations.

Uses the raw Telegram Bot API via httpx - no heavy framework dependency.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from app import db, models
from app.config import settings
from app.ingest import ingest
from app.settlements import apply_settlement, create_group_expense

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.base = f"{API_BASE}/bot{token}"
        self._offset = 0
        self._pending_queue: list[dict] = []  # debounce buffer

    # ---------- Telegram API helpers ----------

    def _get(self, method: str, **params):
        with httpx.Client(timeout=20) as client:
            resp = client.get(f"{self.base}/{method}", params=params)
            resp.raise_for_status()
            return resp.json()

    def _post(self, method: str, json_body: dict):
        with httpx.Client(timeout=20) as client:
            resp = client.post(f"{self.base}/{method}", json=json_body)
            resp.raise_for_status()
            return resp.json()

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        body = {"chat_id": chat_id, "text": text}
        if reply_markup:
            body["reply_markup"] = reply_markup
        return self._post("sendMessage", body)

    def answer_callback(self, callback_id: str, text: str = ""):
        body = {"callback_query_id": callback_id, "text": text}
        return self._post("answerCallbackQuery", body)

    # ---------- Security: allowlist (Section 13) ----------

    def _is_allowed(self, user_id: int) -> bool:
        if not settings.admin_user_id:
            return True  # dev mode - open
        allowed = {int(x) for x in settings.admin_user_id.split(",") if x.strip()}
        return user_id in allowed

    # ---------- Quiet hours (Section 9.4) ----------

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        start = settings.quiet_hours_start
        end = settings.quiet_hours_end
        hour = now.hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # ---------- Polling loop ----------

    def get_updates(self):
        params = {"timeout": 25, "offset": self._offset, "allowed_updates": ["message", "callback_query"]}
        data = self._get("getUpdates", **params)
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            self._handle_update(update)

    def _handle_update(self, update: dict):
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
        except Exception as exc:  # noqa: BLE001 - bot must never die on one bad message
            logger.exception("Bot update handler failed: %s", exc)

    # ---------- Message handling ----------

    def _handle_message(self, message: dict):
        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        if not self._is_allowed(user.get("id", 0)):
            logger.info("Ignoring message from unallowed user %s", user.get("id"))
            return

        text = message.get("text", "")
        if not text:
            self.send_message(chat_id, "Send me a transaction or use a command. /help")
            return

        if text.startswith("/"):
            self._handle_command(chat_id, text)
            return

        # Pending action continuation (e.g. 'group sam 300' after mkgroup)
        if self._handle_pending_continuation(chat_id, text):
            return

        # Free-text transaction -> deterministic parse + optional NLU lane
        self._ingest_text(chat_id, text)

    def _handle_pending_continuation(self, chat_id: int, text: str) -> bool:
        with db.session_scope() as session:
            pa = session.execute(
                select(models.PendingAction)
                .where(models.PendingAction.chat_id == chat_id)
                .order_by(models.PendingAction.id.desc())
            ).scalars().first()
            if not pa:
                return False
            if pa.step == "mkgroup_person":
                self._finish_mkgroup(session, chat_id, pa, text)
                session.delete(pa)
                session.commit()
                return True
            return False

    def _finish_mkgroup(self, session, chat_id: int, pa: models.PendingAction, text: str):
        ctx = json.loads(pa.context_json or "{}")
        txn_id = ctx.get("txn_id")
        txn = session.get(models.Transaction, txn_id)
        if not txn:
            return
        m = re.match(r"(?:group\s+)?(\w+)\s+(\d+(?:\.\d{1,2})?)", text)
        if not m:
            return
        person, share_str = m.group(1), m.group(2)
        share_paise = round(float(share_str) * 100)
        ge = create_group_expense(session, txn, person, share_paise)
        self.send_message(
            chat_id,
            f"Group expense #{ge.id}: {ge.person} owes Rs {ge.expected_receivable / 100:.0f} "
            f"(full {ge.full_amount / 100:.0f}, your share {ge.share_amount / 100:.0f}).",
        )

    def _handle_command(self, chat_id: int, text: str):
        parts = text.split()
        cmd = parts[0].lower()
        if cmd in ("/start", "/help"):
            self.send_message(
                chat_id,
                "Personal Finance Tracker\n\n"
                "Send a transaction: 'dinner 450' or 'group dinner 900 share 300'\n"
                "Commands:\n"
                "/find <query> - search transactions\n"
                "/balances - who owes you\n"
                "/status - this month's summary\n"
                "/month - monthly digest\n"
                "/bills - bills due in next 3 days\n"
                "/test - try a sample transaction",
            )
        elif cmd == "/balances":
            self._send_balances(chat_id)
        elif cmd == "/status":
            self._send_status(chat_id)
        elif cmd == "/find":
            query = " ".join(parts[1:])
            self._send_search(chat_id, query)
        elif cmd == "/month":
            self._send_monthly(chat_id)
        elif cmd == "/bills":
            self._send_due_bills(chat_id)
        elif cmd == "/test":
            self._ingest_text(
                chat_id,
                "Rs.900.00 debited from A/c **1234 by UPI: swiggy. Ref: 999999999999",
            )
        else:
            self.send_message(chat_id, f"Unknown command: {cmd}")

    # ---------- Ingestion path ----------

    def _ingest_text(self, chat_id: int, text: str, source: str = "telegram", channel: str = "telegram"):
        with db.session_scope() as session:
            result = ingest(session, source, text, channel)

            # Graceful degradation: if rules can't parse, try the NLU lane
            # (Section 6.3). AI failure = fall back to rules, never silent wrong.
            if result.outcome == "FAILED":
                from app.nlu import parse_with_llm

                nlu = parse_with_llm(text)
                if nlu and nlu.get("intent") == "group_expense" and nlu.get("amount_paise"):
                    return self._ingest_nlu_group(chat_id, text, nlu)
                if nlu and nlu.get("intent") == "expense" and nlu.get("amount_paise"):
                    rebuilt = self._rebuild_for_rules(text, nlu)
                    result = ingest(session, source, rebuilt, channel)

            txn = result.transaction

            if result.outcome == "FAILED":
                self.send_message(chat_id, f"Could not parse that. Example: 'dinner 450'. ({text[:60]})")
                return

            if result.outcome in ("EXACT", "STRONG"):
                self.send_message(chat_id, f"Duplicate of transaction #{txn.id} - not re-recorded.")
                return

            if result.outcome == "WEAK":
                self.send_message(chat_id, "Possible duplicate - I'll mark it needs_review. /find to check.")
                return

            # NEW transaction
            if result.settlement_candidate:
                ge = result.settlement_candidate
                if self._in_quiet_hours():
                    self.send_message(chat_id, f"Rs {abs(txn.amount_paise) / 100:.0f} received (settlement queued).")
                    return
                markup = {
                    "inline_keyboard": [[
                        {"text": "Confirm settle", "callback_data": f"settle:{txn.id}:{ge.id}"},
                        {"text": "No", "callback_data": f"settle_no:{txn.id}"},
                    ]]
                }
                self.send_message(
                    chat_id,
                    f"Rs {abs(txn.amount_paise) / 100:.0f} received - settle group expense "
                    f"#{ge.id} ({ge.person})?",
                    reply_markup=markup,
                )
                return

            if txn.credit_type == "income":
                self.send_message(chat_id, "Incoming credit recorded (income/reimbursement?).")
                return

            # Trigger-driven shared prompt (Section 7.3)
            if self._should_prompt_shared(txn):
                if self._in_quiet_hours():
                    self.send_message(
                        chat_id,
                        f"Recorded: Rs {abs(txn.amount_paise) / 100:.0f} {txn.counterparty_norm} "
                        f"[{txn.category or 'uncategorized'}]",
                    )
                    return
                markup = {
                    "inline_keyboard": [[
                        {"text": "Shared", "callback_data": f"shared:{txn.id}:yes"},
                        {"text": "Personal", "callback_data": f"shared:{txn.id}:no"},
                    ]]
                }
                self.send_message(
                    chat_id,
                    f"Rs {abs(txn.amount_paise) / 100:.0f} at {txn.counterparty_norm or 'unknown'} "
                    f"({txn.category or 'uncategorized'}) - shared expense?",
                    reply_markup=markup,
                )
            else:
                self.send_message(
                    chat_id,
                    f"Recorded: Rs {abs(txn.amount_paise) / 100:.0f} {txn.counterparty_norm} "
                    f"[{txn.category or 'uncategorized'}]",
                )

    def _should_prompt_shared(self, txn: models.Transaction) -> bool:
        if settings.ask_everything:
            return True
        amount = abs(txn.amount_paise) / 100
        # Odd/non-rounded, known persons, shared categories, above threshold
        if not float(amount).is_integer():
            return True
        if txn.category in ("shared", "food", "groceries", "entertainment", "rent"):
            return True
        if amount >= settings.shared_prompt_threshold:
            return True
        return False

    # ---------- Callback handling (inline keyboards) ----------

    def _handle_callback(self, callback):
        chat_id = callback["message"]["chat"]["id"]
        callback_id = callback["id"]
        data = callback.get("data", "")
        self.answer_callback(callback_id)

        if data.startswith("shared:"):
            _, txn_id, answer = data.split(":")
            self._on_shared_answer(chat_id, int(txn_id), answer == "yes")
        elif data.startswith("mkgroup:"):
            _, txn_id = data.split(":")
            self._on_mkgroup(chat_id, int(txn_id))
        elif data.startswith("skip_group:"):
            _, txn_id = data.split(":")
            self.send_message(chat_id, f"Kept flagged shared - # {txn_id} stays in spend until you settle it.")
        elif data.startswith("settle:"):
            _, txn_id, ge_id = data.split(":")
            self._on_settle(chat_id, int(txn_id), int(ge_id))
        elif data.startswith("settle_no:"):
            _, txn_id = data.split(":")
            self.send_message(chat_id, f"Noted - transaction #{txn_id} is not a settlement.")

    def _on_shared_answer(self, chat_id: int, txn_id: int, shared: bool):
        with db.session_scope() as session:
            txn = session.get(models.Transaction, txn_id)
            if not txn:
                self.send_message(chat_id, "Transaction not found.")
                return
            if shared:
                txn.txn_state = "flagged_shared"
                markup = {
                    "inline_keyboard": [[
                        {"text": "Create group expense", "callback_data": f"mkgroup:{txn_id}"},
                        {"text": "Skip for now", "callback_data": f"skip_group:{txn_id}"},
                    ]]
                }
                self.send_message(
                    chat_id,
                    "Flagged shared - full amount stays in spend until settled.\n"
                    "Create a group expense to make it a receivable (who pays you back?).",
                    reply_markup=markup,
                )
            else:
                txn.txn_state = "personal"
                self.send_message(chat_id, "Marked personal.")
            session.commit()

    def _on_settle(self, chat_id: int, txn_id: int, ge_id: int):
        with db.session_scope() as session:
            txn = session.get(models.Transaction, txn_id)
            ge = session.get(models.GroupExpense, ge_id)
            if not txn or not ge:
                self.send_message(chat_id, "Transaction or group expense not found.")
                return
            state = apply_settlement(session, txn, ge)
            session.commit()
            self.send_message(
                chat_id,
                f"Settled Rs {state['applied'] / 100:.0f}. "
                f"Outstanding: Rs {state['outstanding_after'] / 100:.0f} ({state['status']}).",
            )

    def _on_mkgroup(self, chat_id: int, txn_id: int):
        with db.session_scope() as session:
            txn = session.get(models.Transaction, txn_id)
            if not txn:
                self.send_message(chat_id, "Transaction not found.")
                return
            self.send_message(
                chat_id,
                f"Group expense for #{txn_id} (Rs {abs(txn.amount_paise) / 100:.0f}).\n"
                "Reply with: group <person> <your share>\n"
                "e.g. 'group sam 300' - receivable = full - share.",
            )
            # store pending action
            pa = models.PendingAction(
                chat_id=chat_id, step="mkgroup_person", context_json=json.dumps({"txn_id": txn_id})
            )
            session.add(pa)
            session.commit()

    def _rebuild_for_rules(self, text: str, nlu: dict) -> str:
        """Translate NLU output into canonical text the deterministic parser
        understands. The NLU never creates transactions directly.
        """
        amount = nlu.get("amount_paise", 0) / 100
        direction = nlu.get("direction", "debit")
        words = []
        if direction == "credit":
            words.append("credited")
        if amount:
            words.append(f"{amount:.2f}")
        if nlu.get("counterparty"):
            words.append(f"from {nlu['counterparty']}")
        return " ".join(words) or text

    def _ingest_nlu_group(self, chat_id: int, text: str, nlu: dict):
        """group_expense intent from NLU: flag the spend shared + create the
        receivable directly (full - share), or ask if share is unknown."""
        with db.session_scope() as session:
            result = ingest(session, "telegram", self._rebuild_for_rules(text, nlu), "telegram")
            txn = result.transaction
            if not txn:
                self.send_message(chat_id, "Could not create that group expense - please try again.")
                return
            txn.txn_state = "flagged_shared"
            person = nlu.get("person")
            share = nlu.get("share_paise")
            if person and share:
                ge = create_group_expense(session, txn, person, share)
                session.commit()
                self.send_message(
                    chat_id,
                    f"Group expense #{ge.id}: {ge.person} owes Rs {ge.expected_receivable / 100:.0f} "
                    f"(full {ge.full_amount / 100:.0f}, your share {ge.share_amount / 100:.0f}).",
                )
            else:
                session.commit()
                self._on_shared_answer(chat_id, txn.id, shared=True)

    # ---------- Reports ----------

    def _send_balances(self, chat_id: int):
        with db.session_scope() as session:
            rows = session.execute(
                select(models.GroupExpense)
            ).scalars().all()
        by_person: dict[str, int] = {}
        for ge in rows:
            net = ge.expected_receivable - ge.received_so_far
            by_person[ge.person] = by_person.get(ge.person, 0) + net
        if not by_person:
            self.send_message(chat_id, "No open shared balances.")
            return
        lines = [f"{p}: Rs {net / 100:.0f}" for p, net in sorted(by_person.items()) if net]
        self.send_message(chat_id, "Open balances:\n" + "\n".join(lines) or "All settled!")

    def _send_status(self, chat_id: int):
        with db.session_scope() as session:
            rows = session.execute(
                select(models.Transaction).order_by(models.Transaction.txn_date.desc()).limit(10)
            ).scalars().all()
        if not rows:
            self.send_message(chat_id, "No transactions yet. Send 'dinner 450'.")
            return
        lines = [
            f"#{t.id} Rs {abs(t.amount_paise) / 100:.0f} {t.counterparty_norm} "
            f"[{t.category or '?'}] {t.txn_state}"
            for t in rows
        ]
        self.send_message(chat_id, "Latest transactions:\n" + "\n".join(lines))

    def _send_monthly(self, chat_id: int):
        from app.reports import monthly_digest

        now = datetime.now()
        with db.session_scope() as session:
            digest = monthly_digest(session, now.year, now.month)
        lines = [
            f"Monthly digest {digest['month']}",
            f"Spend: Rs {digest['spend_paise'] / 100:.0f}",
            f"Income: Rs {digest['income_paise'] / 100:.0f}",
            f"Net: Rs {digest['net_paise'] / 100:.0f}",
        ]
        if digest["top_category"]:
            top = digest["top_category"]
            lines.append(f"Top: {top['category']} (Rs {top['spend_paise'] / 100:.0f})")
        self.send_message(chat_id, "\n".join(lines))

    def _send_due_bills(self, chat_id: int):
        from app.reports import due_recurring

        with db.session_scope() as session:
            rows = due_recurring(session)
        if not rows:
            self.send_message(chat_id, "No bills due in the next 3 days.")
            return
        lines = [
            f"Due: {rec.merchant or '?'} Rs {rec.expected_amount / 100:.0f} "
            f"(day {rec.day_of_month})"
            for rec in rows
        ]
        self.send_message(chat_id, "\n".join(lines))

    def _send_search(self, chat_id: int, query: str):
        with db.session_scope() as session:
            q = f"%{query}%"
            rows = session.execute(
                select(models.Transaction)
                .where(models.Transaction.counterparty_norm.ilike(q))
                .order_by(models.Transaction.txn_date.desc())
                .limit(5)
            ).scalars().all()
        if not rows:
            self.send_message(chat_id, f"No matches for '{query}'.")
            return
        lines = [f"#{t.id} {t.txn_date:%d %b} Rs {abs(t.amount_paise) / 100:.0f} {t.counterparty_norm}" for t in rows]
        self.send_message(chat_id, "Matches:\n" + "\n".join(lines))

    # ---------- Debounce (Section 9.3) - SMS floods ----------

    async def _debounced_flush(self, chat_id: int):
        if not self._pending_queue:
            return
        messages = self._pending_queue
        self._pending_queue = []
        text = "\n".join(messages)
        self.send_message(chat_id, text)

    def queue_debounced(self, chat_id: int, text: str):
        """Called by the SMS-forwarding path to batch flood messages."""
        self._pending_queue.append(text)


def run_bot_forever():
    """Entry point - long-polls until interrupted."""
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set - bot disabled.")
        return
    bot = TelegramBot(settings.telegram_bot_token)
    logger.info("Telegram bot polling started")
    while True:
        try:
            bot.get_updates()
        except httpx.HTTPStatusError as exc:
            logger.error("Telegram API error (HTTP %s): %s", exc.response.status_code, exc)
            time.sleep(10)
        except Exception:  # noqa: BLE001
            logger.exception("Polling error - retrying")
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_bot_forever()
