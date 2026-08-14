"""Audit trail for every manual edit/merge/decision (FINAL_PLAN.md 3.4).

The ledger is the source of truth; a manual action that changes state must
leave a who/what/when/why record so nothing is silently mutated.
"""

from __future__ import annotations

from app import models


def audit(
    session,
    actor: str,
    action: str,
    entity: str = "",
    entity_id: int | None = None,
    before: str = "",
    after: str = "",
    reason: str = "",
) -> models.AuditLog:
    entry = models.AuditLog(
        actor=actor,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
    )
    session.add(entry)
    return entry
