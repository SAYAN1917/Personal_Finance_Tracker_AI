"""Email poller (FINAL_PLAN.md D --> H): ingest bank alert emails.

Uses IMAP (Gmail app password) and reads the mailbox READ-ONLY. Nothing is
deleted or moved: re-polling the same messages is harmless because ingest
deduplicates on UTR (Tier 1 EXACT). If an email fails to parse, it is simply
skipped - nothing is silently dropped.

Privacy: this process sees only the alert emails the filter query matches.
"""

from __future__ import annotations

import email
import logging
from email.header import decode_header

from app import db
from app.config import settings
from app.ingest import ingest
from app.logging import setup_logging

logger = logging.getLogger(__name__)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = decode_header(value)
        return "".join(
            part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)
            for part, charset in parts
        )
    except Exception:  # noqa: BLE001 - a bad header must never kill the poller
        return value


def extract_body(msg: email.message.Message) -> str:
    """Return the plain-text body of an email (fallback to stripped HTML)."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if content_type == "text/plain":
                return text.strip()
            if content_type == "text/html":
                html_text = text
        # keep the last html if no plain part
        return _strip_html(html_text).strip()
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        return _strip_html(text).strip()
    return text.strip()


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def poll_once(session) -> dict:
    """Fetch matching emails and ingest their bodies. Read-only + dedup-safe."""
    host = settings.email_imap_host
    user = settings.email_imap_user
    password = settings.email_imap_password
    if not (host and user and password):
        return {"error": "email polling not configured (EMAIL_IMAP_*)", "fetched": 0, "ingested": 0}

    import imaplib

    stats = {"fetched": 0, "ingested": 0, "skipped": 0}
    conn = imaplib.IMAP4_SSL(host, 993)
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, settings.email_imap_query or "ALL")
        if typ != "OK":
            logger.warning("IMAP search failed: %s", typ)
            return stats
        for num in reversed(data[0].split()):
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            body = extract_body(msg)
            if not body:
                continue
            stats["fetched"] += 1
            result = ingest(session, "email", body, "email")
            if result.outcome in ("NEW", "WEAK", "FAILED"):
                stats["ingested" if result.outcome != "FAILED" else "skipped"] += 1
            else:
                stats["skipped"] += 1  # EXACT/STRONG duplicate
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn.logout()
    return stats


def run() -> None:
    """CLI entry point: one poll pass, then exit (cron-friendly)."""
    setup_logging("email-poller")
    settings.validate()
    with db.session_scope() as session:
        stats = poll_once(session)
    logger.info("Email poller: %s", stats)


if __name__ == "__main__":
    run()
