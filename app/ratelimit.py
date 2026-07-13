"""Sliding-window rate limits for trip builds (per user + a global cap).

In-process on purpose: the app runs as a single uvicorn worker, and a trip
build is expensive (several Hermes calls plus a video render), so the caps
are small numbers - no need for Redis.
"""

import time
from collections import deque

from app.config import settings

GLOBAL_WINDOW_SECONDS = 3600

_user_buckets: dict[int, deque[float]] = {}
_global_bucket: deque[float] = deque()


def _prune(bucket: deque[float], now: float, window: float) -> None:
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def check(user_id: int) -> tuple[bool, int]:
    """Try to consume one build slot for this user.

    Returns (allowed, retry_after_seconds). Only successful launches consume
    quota, so callers must check *after* their cheap guards (e.g. the
    one-active-build-per-user check).
    """
    now = time.monotonic()
    window = settings.rate_limit_window_minutes * 60

    _prune(_global_bucket, now, GLOBAL_WINDOW_SECONDS)
    if len(_global_bucket) >= settings.rate_limit_global_per_hour:
        oldest = _global_bucket[0] if _global_bucket else now
        return False, max(1, int(GLOBAL_WINDOW_SECONDS - (now - oldest)) + 1)

    bucket = _user_buckets.setdefault(user_id, deque())
    _prune(bucket, now, window)
    if len(bucket) >= settings.rate_limit_requests:
        oldest = bucket[0] if bucket else now
        return False, max(1, int(window - (now - oldest)) + 1)

    bucket.append(now)
    _global_bucket.append(now)
    return True, 0
