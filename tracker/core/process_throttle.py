"""Process-local TTL gate. Use for "don't run X more than once per N seconds"
patterns where multi-worker duplication is acceptable.

Why not Django cache:
    Django's default cache hits Redis. For hot-path gates that fire on every
    request (visitor pageview broadcast throttle, WS heartbeat SLA check,
    stale-chat cleanup throttle), each request pays 1–2 Redis ops just to
    decide whether to skip the work. On a free Upstash tier that's ~150k
    ops/month wasted.

When *not* to use this:
    - Security rate limits (login attempts, magic-link spam)
    - Billing limits (free-tier visitor cap)
    - Cross-process correctness (e.g. "fire notification once per visitor")
    For those, keep the Redis-backed `django.core.cache` so all workers see
    the same counter.

Trade-off:
    With N gunicorn workers, the "once per N seconds" guarantee becomes
    "once per worker per N seconds", so the work runs up to N× as often.
    That's fine for cleanup jobs and broadcast throttles — the duplicated
    work is cheap and idempotent.

Storage notes:
    Keys + expiry timestamps live in a module-level dict. We sweep expired
    keys lazily on each `should_run` call to keep the dict bounded. There's
    no LRU because the realistic key space is small (per-org / per-visitor).
"""
from __future__ import annotations

import threading
import time

_state: dict[str, float] = {}
_lock = threading.Lock()


def should_run(key: str, interval_seconds: float) -> bool:
    """Returns True the first call for `key`, then False until
    `interval_seconds` have elapsed. Thread-safe."""
    now = time.monotonic()
    with _lock:
        deadline = _state.get(key, 0.0)
        if now < deadline:
            return False
        _state[key] = now + interval_seconds
        # Opportunistic sweep — keeps the dict from growing without bound
        # when many distinct keys are used briefly (e.g. per-visitor gates).
        if len(_state) > 1024:
            _prune_expired_locked(now)
        return True


def mark(key: str, interval_seconds: float) -> None:
    """Force `key` into the "recently-ran" state for `interval_seconds`.
    Used when the caller knows the work just completed elsewhere."""
    with _lock:
        _state[key] = time.monotonic() + interval_seconds


def _prune_expired_locked(now: float) -> None:
    expired = [k for k, d in _state.items() if d <= now]
    for k in expired:
        _state.pop(k, None)
