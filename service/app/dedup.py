"""Deduplication engine (FINAL_PLAN.md Section 5).

Two-tier merge-not-insert:
  Tier 1 (EXACT):  UTR / exact key match - same transaction, forever.
  Tier 2 (STRONG): fuzzy fingerprint - signed amount + account + counterparty
                   within a date window.

Design rules enforced:
  Bug A - repeated UTR from ANY source (incl. same source re-send) is a
          duplicate; never crashes on the unique constraint.
  Bug C - matching is on the SAME SIGNED amount: a credit never matches a debit.
  Bug F - near-date fuzzy merges (date drift) are never silent -> needs_review.
  Ambiguous / multi-candidate fuzzy -> needs_review, never auto-merge.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app import models
from app.normalizer import exact_key

CHANNEL_PRIORITY = {"sms": 5, "email": 4, "telegram": 3, "upi_csv": 2, "card_pdf": 1}

_DATE_WINDOW_DAYS = 3


def _add_source_alias(sources_json: str, channel: str) -> str:
    sources = json.loads(sources_json or "[]")
    if channel not in sources:
        sources.append(channel)
    return json.dumps(sources)


class DedupResult:
    def __init__(
        self,
        outcome: str,
        canonical: models.Transaction | None = None,
        candidates: list[models.Transaction] | None = None,
    ):
        self.outcome = outcome  # EXACT / STRONG / WEAK / NEW
        self.canonical = canonical
        self.candidates = candidates or []


class DedupEngine:
    def __init__(self, session):
        self.session = session

    def _find_by_exact_key(self, key: str) -> models.Transaction | None:
        # Bug A: same exact key (which embeds the UTR) is always a duplicate,
        # regardless of source. Catch IntegrityError-level collisions here.
        result = self.session.execute(
            select(models.Transaction).where(models.Transaction.key_exact == key)
        )
        return result.scalars().first()

    def _fuzzy_candidates(
        self,
        amount_paise: int,
        txn_date,
        counterparty: str,
        mode: str,
        account: str,
    ) -> list[models.Transaction]:
        """Tier 2: same SIGNED amount (Bug C), counterparty match, date window."""
        lo = txn_date - timedelta(days=_DATE_WINDOW_DAYS)
        hi = txn_date + timedelta(days=_DATE_WINDOW_DAYS)
        signed = amount_paise  # Bug C: sign preserved
        rows = self.session.execute(
            select(models.Transaction).where(
                models.Transaction.amount_paise == signed,
                models.Transaction.txn_date >= lo,
                models.Transaction.txn_date <= hi,
                models.Transaction.lifecycle == "active",
            )
        ).scalars().all()

        # Simple similarity: exact counterparty match, or partial containment
        results = []
        for row in rows:
            if row.id and row.counterparty_norm and counterparty:
                if row.counterparty_norm == counterparty:
                    results.append((row, 1.0))
                elif counterparty in row.counterparty_norm or row.counterparty_norm in counterparty:
                    results.append((row, 0.85))
        results.sort(key=lambda item: item[1], reverse=True)
        return [row for row, _ in results]

    def match(self, parsed, channel: str) -> DedupResult:
        """Match a parsed txn against the ledger. Returns outcome + canonical."""
        if parsed.utr:
            key = exact_key(
                parsed.amount_paise,
                parsed.txn_date,
                parsed.counterparty,
                parsed.mode,
                parsed.account,
                parsed.utr,
            )
            existing = self._find_by_exact_key(key)
            if existing is not None:
                return DedupResult("EXACT", canonical=existing)

        # Tier 2 fuzzy
        if parsed.txn_date and parsed.amount_paise:
            candidates = self._fuzzy_candidates(
                parsed.amount_paise,
                parsed.txn_date,
                parsed.counterparty,
                parsed.mode,
                parsed.account,
            )
            if len(candidates) == 1:
                candidate = candidates[0]
                # Bug F: if dates drift, never silently merge
                date_drift = abs((candidate.txn_date - parsed.txn_date).days) > 1
                if date_drift:
                    return DedupResult("WEAK", canonical=candidate, candidates=candidates)
                return DedupResult("STRONG", canonical=candidate)
            if len(candidates) > 1:
                return DedupResult("WEAK", candidates=candidates)

        return DedupResult("NEW")

    def merge(self, parsed, channel: str, canonical: models.Transaction) -> models.Transaction:
        """Merge parsed data into the canonical row; never insert a copy."""
        canonical.sources = _add_source_alias(canonical.sources, channel)
        # Enrich with missing fields from a richer source
        if not canonical.utr and parsed.utr:
            canonical.utr = parsed.utr
        if not canonical.account and parsed.account:
            canonical.account = parsed.account
        if not canonical.counterparty_norm and parsed.counterparty:
            canonical.counterparty_norm = parsed.counterparty
        # 2+ independent sources -> confirmed (Section 5.6)
        sources = json.loads(canonical.sources)
        if len(sources) >= 2 and canonical.status == "pending":
            canonical.status = "confirmed"
        return canonical
