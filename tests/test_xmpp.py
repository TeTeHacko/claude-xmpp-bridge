"""Tests for XMPPConnection — reconnect logic and backoff."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from claude_xmpp_bridge.xmpp import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    BACKOFF_MULTIPLIER,
    XMPPConnection,
)


class TestSendReturnValue:
    """send() must return bool indicating success."""

    def test_send_returns_true_when_connected(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()
        conn.connected.set()

        result = conn.send("user@example.com", "hello")

        assert result is True
        conn._bot.make_message.assert_called_once()

    def test_send_returns_false_when_not_connected(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()
        # connected is NOT set

        result = conn.send("user@example.com", "hello")

        assert result is False
        conn._bot.make_message.assert_not_called()

    def test_send_returns_false_when_no_bot(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn.connected.set()
        # _bot is None

        result = conn.send("user@example.com", "hello")

        assert result is False


class TestBackoffEscalation:
    """Backoff should escalate: 5 → 10 → 20 → 40 → 60 → 60."""

    async def test_backoff_escalates(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()

        delays: list[float] = []

        with patch("claude_xmpp_bridge.xmpp.asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None

            for _ in range(6):
                delays.append(conn._backoff)
                # Simulate what _on_disconnected does to backoff
                conn._backoff = min(conn._backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX)

        assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


class TestBackoffReset:
    """Backoff resets to initial on successful reconnect."""

    async def test_session_start_resets_backoff(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()
        # Simulate escalated backoff
        conn._backoff = 40.0

        await conn._on_session_start(None)

        assert conn._backoff == BACKOFF_INITIAL
        assert conn.connected.is_set()


class TestDisconnectedReconnects:
    """_on_disconnected should sleep and then reconnect."""

    async def test_on_disconnected_reconnects(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()
        conn.connected.set()
        initial_backoff = conn._backoff

        with patch("claude_xmpp_bridge.xmpp.asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            await conn._on_disconnected(None)

        assert not conn.connected.is_set()
        mock_sleep.assert_called_once_with(initial_backoff)
        conn._bot.connect.assert_called_once()

    async def test_on_disconnected_escalates_backoff(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()

        with patch("claude_xmpp_bridge.xmpp.asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            await conn._on_disconnected(None)

        assert conn._backoff == BACKOFF_INITIAL * BACKOFF_MULTIPLIER


class TestDisconnectNoReconnect:
    """disconnect() sets _should_reconnect=False, preventing reconnect."""

    async def test_disconnect_prevents_reconnect(self):
        conn = XMPPConnection("bot@example.com", "secret")
        bot = MagicMock()
        conn._bot = bot

        conn.disconnect()

        assert conn._bot is None
        assert conn._should_reconnect is False
        bot.disconnect.assert_called_once()

    async def test_on_disconnected_skips_when_should_reconnect_false(self):
        """After disconnect(), _on_disconnected returns immediately — no sleep, no reconnect."""
        conn = XMPPConnection("bot@example.com", "secret")
        conn._bot = MagicMock()
        conn.connected.set()
        conn._should_reconnect = False

        with patch("claude_xmpp_bridge.xmpp.asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            await conn._on_disconnected(None)

        assert not conn.connected.is_set()
        mock_sleep.assert_not_called()
        conn._bot.connect.assert_not_called()
