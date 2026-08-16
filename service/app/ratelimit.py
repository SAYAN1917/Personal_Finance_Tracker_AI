"""Small in-process rate limiter for webhook endpoints.

Single-user app, so a simple per-key sliding window in memory is enough - no
external store needed. Keys on the caller IP (or webhook secret) to stop a
flood of fake transactions filling the ledger. Per-process: if you run more
than one worker, put a reverse proxy (nginx) rate limit in front instead.
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request

from app.config import settings

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_WINDOW_SECONDS = 60


def _rate_key(request: Request) -> str:
    secret = request.headers.get("X-Webhook-Secret") or request.headers.get("Authorization", "")
    return secret or request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    """FastAPI dependency: 429 when the caller exceeds the window budget.

    Off by default (RATE_LIMIT_PER_MIN=0). Production sets it explicitly.
    """
    if settings.rate_limit_per_min <= 0:
        return
    key = _rate_key(request)
    now = time.monotonic()
    with _lock:
        window = [t for t in _hits.get(key, []) if now - t < _WINDOW_SECONDS]
        if len(window) >= settings.rate_limit_per_min:
            _hits[key] = window
            raise HTTPException(status_code=429, detail="Rate limit exceeded - try again later")
        window.append(now)
        _hits[key] = window
