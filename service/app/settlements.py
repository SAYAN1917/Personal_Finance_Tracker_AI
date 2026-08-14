"""Settlement matching (FINAL_PLAN.md Section 7.4).

Conservative by design:
  - Only matches an actual `group_expense` record (flag != receivable).
  - Auto-suggest only when exactly ONE open candidate matches, OR the sender
    is a known person with open group expenses.
  - Ambiguous (two groups expecting the same amount, or unknown sender) ->
    leave unmatched; a human picks (Prototype Bug B).
"""

from __future__ import annotations

from sqlalchemy import select

from app import models


def find_settlement_candidates(
    session,
    amount_paise: int,
    counterparty: str,
) -> list[models.GroupExpense]:
    """Return open group expenses that could be settled by an inbound credit.

    Matching is by person (VPA local part) OR by expected receivable amount.
    Both signals used conservatively.
    """
    amount = abs(amount_paise)
    rows = session.execute(
        select(models.GroupExpense).where(
            models.GroupExpense.status.in_(["open", "partial"])
        )
    ).scalars().all()

    person = (counterparty or "").strip().lower()
    candidates = []
    for ge in rows:
        ge_person = ge.person.strip().lower()
        # Precedence: person must be non-empty before any containment test.
        # `ge_person in person` on an empty `person` would falsely match "".
        if person and (person in ge_person or ge_person in person):
            candidates.append((ge, 2.0))
        elif ge.expected_receivable == amount:
            candidates.append((ge, 1.0))
    candidates.sort(key=lambda item: item[1], reverse=True)
    return [ge for ge, _ in candidates]


def suggest_settlement(
    session,
    amount_paise: int,
    counterparty: str,
) -> models.GroupExpense | None:
    """Return the single safe settlement candidate, or None if ambiguous."""
    candidates = find_settlement_candidates(session, amount_paise, counterparty)
    if len(candidates) == 1:
        return candidates[0]
    return None


def create_group_expense(
    session,
    transaction,
    person: str,
    share_paise: int | None = None,
) -> models.GroupExpense:
    """Explicitly turn a shared expense into a receivable (Section 7.3).

    Receivable = full amount - your share. Only the explicit path can create
    a group expense; a shared flag alone is never a receivable.

    share_paise is the user's OWN share. None or 0 means the share is not
    known yet (flag-only) - the receivable is deferred until settlement
    (Section 7.3 deferred math). It must NOT be interpreted as 'friend owes
    the full amount'.
    """
    full = abs(transaction.amount_paise)
    if share_paise is not None and share_paise > 0:
        share = min(share_paise, full)
        receivable = max(0, full - share)
    else:
        # Flag-only: share unknown -> deferred, computed at settlement
        share = None
        receivable = 0

    ge = models.GroupExpense(
        transaction_id=transaction.id,
        person=person.strip().lower(),
        full_amount=full,
        share_amount=share,
        expected_receivable=receivable,
        received_so_far=0,
        status="open",
    )
    session.add(ge)
    transaction.txn_state = "flagged_shared"
    session.add(transaction)
    session.flush()  # assign ge.id
    return ge


def apply_settlement(session, transaction, group_expense, amount_paise=None) -> dict:
    """Apply an inbound credit against a group expense. Returns updated state."""
    amount = amount_paise or abs(transaction.amount_paise)
    outstanding = group_expense.expected_receivable - group_expense.received_so_far
    applied = min(amount, outstanding)

    settlement = models.Settlement(
        txn_id=transaction.id,
        group_expense_id=group_expense.id,
        amount=applied,
        kind="full" if applied >= outstanding else "partial",
    )
    session.add(settlement)

    group_expense.received_so_far += applied
    if group_expense.received_so_far >= group_expense.expected_receivable:
        group_expense.status = "settled"
    else:
        group_expense.status = "partial"

    # The inbound credit is a reimbursement, never income
    transaction.credit_type = "reimbursement"
    transaction.txn_state = "resolved_shared"
    transaction.status = "confirmed"

    session.add(group_expense)
    session.add(transaction)
    return {
        "applied": applied,
        "outstanding_after": group_expense.expected_receivable - group_expense.received_so_far,
        "status": group_expense.status,
    }
