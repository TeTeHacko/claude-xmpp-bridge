"""Tests for notify module — fire-and-forget XMPP message sending."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from claude_xmpp_bridge.config import NotifyConfig
from claude_xmpp_bridge.notify import send_notification


def _make_config() -> NotifyConfig:
    return NotifyConfig(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
    )


# Keyword args to make tests fast (skip real 30s connection timeout & 1s grace sleep).
_FAST = {"connection_timeout": 0.1, "disconnect_grace": 0}


class TestSendNotification:
    """send_notification must connect, send, and disconnect."""

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_creates_connection_with_jid_and_password(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        MockXMPP.return_value = conn

        await send_notification(config, "hello", **_FAST)

        MockXMPP.assert_called_once_with("bot@example.com", "secret")

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_calls_start(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        MockXMPP.return_value = conn

        await send_notification(config, "hello", **_FAST)

        conn.start.assert_called_once()

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_waits_for_connected(self, MockXMPP):
        """connected.wait() must be awaited before sending."""
        config = _make_config()
        conn = MagicMock()
        connected_event = asyncio.Event()
        conn.connected = connected_event
        MockXMPP.return_value = conn

        # Set the event after a brief delay to prove we actually wait
        async def set_later():
            await asyncio.sleep(0.01)
            connected_event.set()

        asyncio.get_event_loop().create_task(set_later())
        await send_notification(config, "hello", **_FAST)

        # If we got here without hanging, connected.wait() was properly awaited
        conn.send.assert_called_once()

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_sends_message_to_recipient(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        MockXMPP.return_value = conn

        await send_notification(config, "test message", **_FAST)

        conn.send.assert_called_once_with("user@example.com", "test message")

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_disconnects_after_send(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        MockXMPP.return_value = conn

        await send_notification(config, "hello", **_FAST)

        conn.disconnect.assert_called_once()

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_full_sequence_order(self, MockXMPP):
        """Verify the correct order: start → wait → send → disconnect."""
        config = _make_config()
        call_order = []

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.start.side_effect = lambda: call_order.append("start")
        def _send(r: str, m: str) -> bool:
            call_order.append("send")
            return True

        conn.send.side_effect = _send
        conn.disconnect.side_effect = lambda: call_order.append("disconnect")
        MockXMPP.return_value = conn

        await send_notification(config, "hello", **_FAST)

        assert call_order == ["start", "send", "disconnect"]


class TestConnectionTimeout:
    """Connection timeout must raise ConnectionError, not raw TimeoutError."""

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_timeout_raises_connection_error(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        # Never set → will timeout
        MockXMPP.return_value = conn

        with pytest.raises(ConnectionError, match="XMPP connection timeout"):
            await send_notification(config, "hello", **_FAST)

        conn.disconnect.assert_called_once()

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_timeout_does_not_raise_raw_timeout_error(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        MockXMPP.return_value = conn

        with pytest.raises(ConnectionError):
            await send_notification(config, "hello", **_FAST)


class TestSendFailure:
    """send() returning False must raise ConnectionError."""

    @patch("claude_xmpp_bridge.notify.XMPPConnection")
    async def test_send_failure_raises_connection_error(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = False
        MockXMPP.return_value = conn

        with pytest.raises(ConnectionError, match="send failed"):
            await send_notification(config, "hello", **_FAST)

        conn.disconnect.assert_called_once()
