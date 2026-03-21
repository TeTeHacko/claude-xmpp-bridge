"""Tests for claude_xmpp_bridge.rate_limit."""

from __future__ import annotations

from unittest.mock import patch

from claude_xmpp_bridge.rate_limit import RateLimiter


class TestRateLimiter:
    """Unit tests for the sliding-window RateLimiter."""

    def test_allows_requests_within_limit(self):
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            allowed, retry_after = rl.check("key1")
            assert allowed is True
            assert retry_after == 0.0

    def test_rejects_after_limit(self):
        rl = RateLimiter(max_per_minute=3)
        for _ in range(3):
            rl.check("key1")
        allowed, retry_after = rl.check("key1")
        assert allowed is False
        assert retry_after > 0

    def test_separate_keys_tracked_independently(self):
        rl = RateLimiter(max_per_minute=2)
        rl.check("a")
        rl.check("a")
        allowed_a, _ = rl.check("a")
        assert allowed_a is False
        allowed_b, _ = rl.check("b")
        assert allowed_b is True

    def test_expired_entries_pruned(self):
        rl = RateLimiter(max_per_minute=2)
        # Manually add old entries
        with patch("claude_xmpp_bridge.rate_limit.time.monotonic", return_value=100.0):
            rl.check("k")
            rl.check("k")
        # Now at time 200 (well past 60s window)
        with patch("claude_xmpp_bridge.rate_limit.time.monotonic", return_value=200.0):
            allowed, _ = rl.check("k")
            assert allowed is True

    def test_cleanup_removes_inactive_keys(self):
        rl = RateLimiter(max_per_minute=10)
        rl.check("active")
        rl.check("stale")
        removed = rl.cleanup(active_keys={"active"})
        assert removed == 1
        assert "stale" not in rl._buckets
        assert "active" in rl._buckets

    def test_cleanup_without_active_keys_removes_empty_buckets(self):
        rl = RateLimiter(max_per_minute=10)
        rl._buckets["empty"] = __import__("collections").deque()
        rl.check("nonempty")
        removed = rl.cleanup()
        assert removed == 1
        assert "empty" not in rl._buckets
        assert "nonempty" in rl._buckets

    def test_retry_after_positive(self):
        rl = RateLimiter(max_per_minute=1)
        rl.check("k")
        _, retry_after = rl.check("k")
        assert retry_after >= 0.1
