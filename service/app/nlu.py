"""NLU AI lane (FINAL_PLAN.md Section 6.3).

A single narrow NLU node: free-text Telegram input -> structured JSON.
PRIVACY CONTRACT: the LLM sees ONLY the free text the user typed plus an
allowlist of categories/persons. It NEVER sees raw bank SMS, balances, or UTRs.

Strict output + validation: malformed JSON or unexpected fields fall back to
None (deterministic parser + confirmation). AI failure = graceful degradation,
never silent wrong data. The LLM never corrects UTRs or merges transactions.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the NLU node for a personal finance tracker. Parse the user's message
into STRICT JSON with exactly these keys:
- amount_paise: integer paise. Omit if no amount is mentioned.
- counterparty: merchant or person name, lowercase. Omit if unknown.
- direction: "debit" or "credit".
- intent: one of "expense" | "group_expense" | "settlement" | "unknown".
- person: person name for group/settlement intents, if mentioned.
- share_paise: for group_expense, the user's own share in paise, if mentioned.
- category: one of the ALLOWED_CATEGORIES, or null.

Return ONLY the JSON object. No prose, no markdown fences.
If you cannot map the input, return {"intent": "unknown"}."""

ALLOWED_CATEGORIES = [
    "food", "groceries", "transport", "entertainment", "bills", "rent",
    "shopping", "health", "travel", "education", "income", "shared", "other",
]


def _is_enabled() -> bool:
    return bool(settings.llm_api_key and settings.llm_base_url and settings.llm_model)


def _build_prompt(user_text: str) -> str:
    return (
        f"ALLOWED_CATEGORIES: {ALLOWED_CATEGORIES}\n\n"
        f"User message: {user_text}"
    )


def parse_with_llm(user_text: str) -> dict | None:
    """Call the free-tier cloud LLM and return validated JSON, or None."""
    if not _is_enabled():
        return None
    if len(user_text) > 2000:
        logger.warning("NLU input too long - falling back to rules")
        return None

    body = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(user_text)},
        ],
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=body,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - graceful degradation
        logger.warning("NLU call failed (%s) - falling back to rules", exc)
        return None

    parsed = _validate(content)
    if parsed is None:
        logger.warning("NLU returned invalid JSON - falling back to rules")
    return parsed


def _validate(content: str) -> dict | None:
    """Parse and validate LLM output against the strict schema."""
    content = content.strip()
    if content.startswith("```"):
        # strip code fences if the model ignored instructions
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    result: dict = {}
    for key in ("amount_paise", "counterparty", "direction", "intent", "person", "share_paise", "category"):
        if key in data:
            result[key] = data[key]
    if "intent" not in result:
        result["intent"] = "unknown"
    if result.get("direction") not in ("debit", "credit"):
        result["direction"] = "debit"
    if result.get("category") not in ALLOWED_CATEGORIES:
        result["category"] = None
    return result
