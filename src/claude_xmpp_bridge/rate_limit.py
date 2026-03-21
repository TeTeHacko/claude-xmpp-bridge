"""Simple sliding-window rate limiter for socket and MCP requests."""

from __future__ import annotations

import time
from collections import deque

# Defaults — can be overridden by callers.
SOCKET_MAX_PER_MINUTE = 300
MCP_MAX_PER_MINUTE = 600

# Window size in seconds.
_WINDOW = 60.0


class RateLimiter:
    """Token-bucket-style rate limiter using a sliding time window.

    Tracks request timestamps per key (typically session_id or client_id).
    Each call to :meth:`check` prunes expired timestamps and returns
    whether the request is allowed.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """Check if *key* is within the rate limit.

        Returns:
            ``(allowed, retry_after)`` — *allowed* is True if the request
            should proceed, *retry_after* is 0.0 when allowed or the
            number of seconds until the oldest entry expires.
        """
        now = time.monotonic()
        cutoff = now - _WINDOW

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket

        # Prune expired entries
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self._max:
            retry_after = round(bucket[0] - cutoff, 1)
            return False, max(retry_after, 0.1)

        bucket.append(now)
        return True, 0.0

    def cleanup(self, active_keys: set[str] | None = None) -> int:
        """Remove buckets for inactive keys.

        If *active_keys* is provided, removes all buckets whose key is not
        in the set.  Otherwise removes only empty buckets.

        Returns the number of removed buckets.
        """
        if active_keys is not None:
            stale = [k for k in self._buckets if k not in active_keys]
        else:
            stale = [k for k, v in self._buckets.items() if not v]
        for k in stale:
            del self._buckets[k]
        return len(stale)
