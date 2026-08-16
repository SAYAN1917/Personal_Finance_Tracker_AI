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
from app.audit import audit
from app.config import settings
from app.ingest import ingest
from app.logging import setup_logging
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
        # Fail closed: no allowlist configured => reject everyone.
        if not settings.admin_user_id:
            return False
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
        # Flush batched SMS-forward confirmations at the end of the poll batch.
        self._flush_all_debounced()

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
                done = self._finish_mkgroup(session, chat_id, pa, text)
                if done:
                    session.delete(pa)
                    session.commit()
                return True
            return False

    def _finish_mkgroup(self, session, chat_id: int, pa: models.PendingAction, text: str) -> bool:
        ctx = json.loads(pa.context_json or "{}")
        txn_id = ctx.get("txn_id")
        txn = session.get(models.Transaction, txn_id)
        if not txn:
            self.send_message(chat_id, "Transaction not found - please start again.")
            return False
        # Share is optional: 'group sam' defers the share to settlement;
        # 'group sam 300' computes receivable = full - share now.
        m = re.match(r"(?:group\s+)?([a-zA-Z][a-zA-Z0-9_.]*)\s*(\d+(?:\.\d{1,2})?)?", text)
        if not m:
            self.send_message(
                chat_id,
                "Reply with: group <person> [your share]\n"
                "e.g. 'group sam 300' or 'group sam' (share computed at settlement).",
            )
            return False
        person, share_str = m.group(1), m.group(2)
        share_paise = round(float(share_str) * 100) if share_str else None
        ge = create_group_expense(session, txn, person, share_paise)
        audit(
            session,
            "bot",
            "group_expense",
            "group_expense",
            ge.id,
            before=f"txn #{txn.id} flagged_shared",
            after=f"receivable {ge.expected_receivable}",
            reason=f"person={ge.person}",
        )
        if ge.share_amount is None:
            note = "share unknown - receivable computed at settlement"
        else:
            note = f"your share {ge.share_amount / 100:.0f}"
        self.send_message(
            chat_id,
            f"Group expense #{ge.id}: {ge.person} owes Rs {ge.expected_receivable / 100:.0f} "
            f"(full {ge.full_amount / 100:.0f}, {note}).",
        )
        return True

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
                "/person <name> - add a known person\n"
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
        elif cmd == "/person":
            name = " ".join(parts[1:]).strip()
            if not name:
                self.send_message(chat_id, "Usage: /person <name>\ne.g. /person sam")
                return
            with db.session_scope() as session:
                from app.settlements import learn_person

                learn_person(session, name)
                session.commit()
            self.send_message(chat_id, f"Added {name} to known persons.")
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

            # NLU enrichment (Section 6.3): a partially-parsed NEW free-text
            # entry can still be enriched - category when rules left it
            # unknown, or a group_expense intent ('dinner 450 sam').
            if result.outcome == "NEW" and source == "telegram" and txn is not None:
                if self._enrich_nlu(session, chat_id, text, txn):
                    return

            if result.outcome == "FAILED":
                self.send_message(chat_id, f"Could not parse that. Example: 'dinner 450'. ({text[:60]})")
                return

            if result.outcome in ("EXACT", "STRONG"):
                self.send_message(chat_id, f"Duplicate of transaction #{txn.id} - not re-recorded.")
                return

            if result.outcome == "WEAK":
                if result.duplicate_of is None:
                    self.send_message(
                        chat_id,
                        "Possible duplicate - stored with needs_review (not merged). "
                        "Use /find or /status to inspect it.",
                    )
                    return
                markup = {
                    "inline_keyboard": [[
                        {"text": "Merge", "callback_data": f"weakmerge:{txn.id}:{result.duplicate_of.id}"},
                        {"text": "No", "callback_data": f"weakkeep:{txn.id}"},
                    ]]
                }
                self.send_message(
                    chat_id,
                    f"Possible duplicate of #{result.duplicate_of.id} "
                    f"({abs(result.duplicate_of.amount_paise) / 100:.0f} {result.duplicate_of.counterparty_norm}). "
                    "Merge into it, or keep as a separate transaction?",
                    reply_markup=markup,
                )
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
                self._confirm(chat_id, "Incoming credit recorded (income/reimbursement?).", text)
                return

            if txn.category == "transfer":
                self._confirm(
                    chat_id,
                    f"Transfer recorded (not spend): Rs {abs(txn.amount_paise) / 100:.0f} "
                    f"{txn.counterparty_norm or ''}".rstrip(),
                    text,
                )
                return

            if txn.category == "emi":
                self._confirm(
                    chat_id,
                    f"EMI recorded (excluded from spend): Rs {abs(txn.amount_paise) / 100:.0f} "
                    f"{txn.counterparty_norm or ''}".rstrip(),
                    text,
                )
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
                self._confirm(
                    chat_id,
                    f"Recorded: Rs {abs(txn.amount_paise) / 100:.0f} {txn.counterparty_norm} "
                    f"[{txn.category or 'uncategorized'}]",
                    text,
                )

    def _should_prompt_shared(self, txn: models.Transaction) -> bool:
        if settings.ask_everything:
            return True
        if txn.category in ("transfer", "emi"):
            return False
        from app.classify import is_shared_heuristic

        return is_shared_heuristic(
            txn.amount_paise,
            txn.counterparty_norm or "",
            txn.category,
            settings.shared_prompt_threshold,
        )

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
        elif data.startswith("weakmerge:"):
            _, incoming_id, canonical_id = data.split(":")
            self._on_weak_merge(chat_id, int(incoming_id), int(canonical_id))
        elif data.startswith("weakkeep:"):
            _, incoming_id = data.split(":")
            self._on_weak_keep(chat_id, int(incoming_id))

    def _on_weak_merge(self, chat_id: int, incoming_id: int, canonical_id: int):
        with db.session_scope() as session:
            incoming = session.get(models.Transaction, incoming_id)
            canonical = session.get(models.Transaction, canonical_id)
            if not incoming or not canonical:
                self.send_message(chat_id, "Transaction not found - it may have been merged already.")
                return
            from app.dedup import merge_transaction

            merge_transaction(session, incoming, canonical)
            audit(session, "bot", "merge", "transaction", incoming.id, before="review", after=f"merged into #{canonical.id}")
            session.commit()
            self.send_message(chat_id, f"Merged into #{canonical_id}.")

    def _on_weak_keep(self, chat_id: int, incoming_id: int):
        with db.session_scope() as session:
            txn = session.get(models.Transaction, incoming_id)
            if not txn:
                self.send_message(chat_id, "Transaction not found.")
                return
            txn.needs_review = False
            txn.status = "pending"
            audit(session, "bot", "merge_no", "transaction", txn.id, before="needs_review", after="kept distinct")
            session.commit()
            self.send_message(chat_id, f"Kept #{txn.id} as a distinct transaction.")

    def _on_shared_answer(self, chat_id: int, txn_id: int, shared: bool):
        with db.session_scope() as session:
            txn = session.get(models.Transaction, txn_id)
            if not txn:
                self.send_message(chat_id, "Transaction not found.")
                return
            if shared:
                txn.txn_state = "flagged_shared"
                txn.needs_review = False
                audit(session, "bot", "shared_prompt", "transaction", txn.id, before="personal", after="flagged_shared")
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
                txn.needs_review = False
                audit(session, "bot", "shared_prompt", "transaction", txn.id, before="flagged_shared", after="personal")
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
            audit(
                session,
                "bot",
                "settle",
                "settlement",
                txn.id,
                before="pending",
                after=f"applied {state['applied']}",
                reason=f"ge #{ge.id}",
            )
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

    def _enrich_nlu(self, session, chat_id: int, text: str, txn: models.Transaction) -> bool:
        """Section 6.3: enrich a partially-parsed NEW free-text entry.

        Returns True when the message was fully handled here (group expense),
        False to let the normal NEW dispatch continue.
        """
        from app.nlu import parse_with_llm

        nlu = parse_with_llm(text)
        if not nlu:
            return False

        # Category: only fill when rules left it unknown - never override.
        category = nlu.get("category")
        if category and category != "unknown" and txn.category is None:
            txn.category = category
            session.add(txn)

        # Group expense intent: 'dinner 450 sam' -> flag shared + receivable.
        if nlu.get("intent") == "group_expense" and nlu.get("person"):
            txn.txn_state = "flagged_shared"
            txn.needs_review = False
            session.add(txn)
            session.commit()
            person = nlu.get("person")
            share = nlu.get("share_paise")
            if person and share:
                from app.settlements import create_group_expense

                ge = create_group_expense(session, txn, person, share)
                session.commit()
                self.send_message(
                    chat_id,
                    f"Group expense #{ge.id}: {ge.person} owes Rs {ge.expected_receivable / 100:.0f} "
                    f"(full {ge.full_amount / 100:.0f}).",
                )
            else:
                self._on_mkgroup(chat_id, txn.id)
            return True

        session.commit()
        return False

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

    @staticmethod
    def _looks_like_sms(text: str) -> bool:
        t = text.lower()
        return ("rs" in t or "inr" in t or "₹" in t) and (
            "debited" in t or "credited" in t or "ref" in t
        )

    def _confirm(self, chat_id: int, text: str, source_text: str):
        """Send a plain confirmation, batched when it's a forwarded SMS."""
        if self._looks_like_sms(source_text):
            self.queue_debounced(chat_id, text)
        else:
            self.send_message(chat_id, text)

    def _flush_all_debounced(self):
        chats = {chat_id for chat_id, _ in self._pending_queue}
        for chat_id in chats:
            self._debounced_flush(chat_id)

    def _debounced_flush(self, chat_id: int):
        if not self._pending_queue:
            return
        to_flush = [text for c, text in self._pending_queue if c == chat_id]
        if not to_flush:
            return
        self._pending_queue = [(c, t) for c, t in self._pending_queue if c != chat_id]
        text = "\n".join(f"- {m}" for m in to_flush)
        self.send_message(chat_id, text)

    def queue_debounced(self, chat_id: int, text: str):
        """Called by the SMS-forwarding path to batch flood messages."""
        self._pending_queue.append((chat_id, text))


def run_bot_forever():
    """Entry point - long-polls until interrupted."""
    setup_logging("bot")
    settings.validate(require_bot=True)  # fail fast on missing prod secrets
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set - bot disabled.")
        return
    if not settings.admin_user_id:
        logger.warning("ADMIN_USER_ID not set - bot will reject ALL users (fail closed).")
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
    run_bot_forever()
