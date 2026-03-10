"""Tests for XMPPConnection — reconnect logic and backoff."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


class TestIsConnectedProperty:
    """is_connected property mirrors connected.is_set()."""

    def test_is_connected_false_initially(self):
        conn = XMPPConnection("bot@example.com", "secret")
        assert conn.is_connected is False

    def test_is_connected_true_when_event_set(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn.connected.set()
        assert conn.is_connected is True

    def test_is_connected_false_after_clear(self):
        conn = XMPPConnection("bot@example.com", "secret")
        conn.connected.set()
        conn.connected.clear()
        assert conn.is_connected is False


class TestOnMessageCallback:
    """_on_message dispatches to the registered callback."""

    async def test_on_message_calls_callback(self):
        conn = XMPPConnection("bot@example.com", "secret")
        callback = AsyncMock()
        conn.on_message(callback)
        msg = MagicMock()

        await conn._on_message(msg)

        callback.assert_called_once_with(msg)

    async def test_on_message_no_callback_is_safe(self):
        """_on_message without a registered callback must not raise."""
        conn = XMPPConnection("bot@example.com", "secret")
        msg = MagicMock()
        # Should not raise
        await conn._on_message(msg)

    async def test_on_message_replaced_callback(self):
        """Only the last registered callback should be called."""
        conn = XMPPConnection("bot@example.com", "secret")
        first = AsyncMock()
        second = AsyncMock()
        conn.on_message(first)
        conn.on_message(second)
        msg = MagicMock()

        await conn._on_message(msg)

        first.assert_not_called()
        second.assert_called_once_with(msg)


class TestSendHtmlStripping:
    """send() must strip the html stanza from the message."""

    def test_send_deletes_html_key(self):
        conn = XMPPConnection("bot@example.com", "secret")
        mock_msg = MagicMock()
        mock_bot = MagicMock()
        mock_bot.make_message.return_value = mock_msg
        conn._bot = mock_bot
        conn.connected.set()

        conn.send("user@example.com", "hello")

        # del msg["html"] should have been called
        mock_msg.__delitem__.assert_called_once_with("html")
        mock_msg.send.assert_called_once()


class TestStart:
    """start() wires up event handlers and calls bot.connect()."""

    def test_start_calls_connect(self):
        conn = XMPPConnection("bot@example.com", "secret")
        mock_bot = MagicMock()

        with patch("claude_xmpp_bridge.xmpp.slixmpp.ClientXMPP", return_value=mock_bot):
            conn.start()

        mock_bot.connect.assert_called_once()

    def test_start_registers_three_handlers(self):
        conn = XMPPConnection("bot@example.com", "secret")
        mock_bot = MagicMock()

        with patch("claude_xmpp_bridge.xmpp.slixmpp.ClientXMPP", return_value=mock_bot):
            conn.start()

        assert mock_bot.add_event_handler.call_count == 3
        events = [c.args[0] for c in mock_bot.add_event_handler.call_args_list]
        assert "session_start" in events
        assert "message" in events
        assert "disconnected" in events

    def test_start_force_starttls_disables_plain(self):
        conn = XMPPConnection("bot@example.com", "secret", force_starttls=True)
        mock_bot = MagicMock()
        mock_bot.__getitem__ = MagicMock(return_value=MagicMock())

        with patch("claude_xmpp_bridge.xmpp.slixmpp.ClientXMPP", return_value=mock_bot):
            conn.start()

        # feature_mechanisms plugin accessed via mock_bot["feature_mechanisms"]
        mock_bot.__getitem__.assert_any_call("feature_mechanisms")

    def test_start_no_starttls_skips_plain_disable(self):
        conn = XMPPConnection("bot@example.com", "secret", force_starttls=False)
        mock_bot = MagicMock()

        with patch("claude_xmpp_bridge.xmpp.slixmpp.ClientXMPP", return_value=mock_bot):
            conn.start()

        mock_bot.__getitem__.assert_not_called()
