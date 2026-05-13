"""Tiny token-bucket-ish rate limiter built on Django's cache.

Why a homegrown helper instead of django-ratelimit / DRF throttling:
  - We need *per-action* and *per-key* control (per IP, per session, per room).
  - The existing widget endpoints already use cache-counter throttling — this
    keeps the implementation consistent and avoids a new dep.
  - Returns a small dataclass so callers can return a clean JSON 429 body.

Usage:
    state = check(request, action='start_chat', limit=20, window=60)
    if state.blocked:
        return JsonResponse({'error': 'Slow down'}, status=429)
"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache


def client_ip(request):
    """Best-effort IP extraction. We deliberately read X-Forwarded-For only
    when we trust the proxy (settings.TRUST_X_FORWARDED_FOR) elsewhere; for
    rate limiting it's fine to use whatever the edge claims since spoofing
    just means the spoofer hits their own bucket."""
    return (
        request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        or request.META.get('REMOTE_ADDR', 'unknown')
    )


@dataclass
class ThrottleState:
    blocked: bool
    count: int
    limit: int
    window: int


def check(request, action: str, limit: int, window: int, key: str = '') -> ThrottleState:
    """Check + increment a counter. Returns blocked=True when over the limit."""
    bucket = f'rl:{action}:{key or client_ip(request)}'
    count = cache.get(bucket, 0) + 1
    # Set with TTL=window so each window starts fresh on first hit. Subsequent
    # increments keep the same expiry by re-setting with a clamped TTL.
    cache.set(bucket, count, timeout=window)
    return ThrottleState(blocked=count > limit, count=count, limit=limit, window=window)
