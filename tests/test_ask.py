"""Tests for ask module — send XMPP message and wait for reply."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_slixmpp_message as _make_slixmpp_message

from claude_xmpp_bridge.ask import send_and_wait
from claude_xmpp_bridge.config import NotifyConfig


def _make_config() -> NotifyConfig:
    return NotifyConfig(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
    )


# Keyword args to make tests fast (skip real 30s connection timeout & 1s grace sleep).
_FAST = {"connection_timeout": 0.1, "disconnect_grace": 0}


class TestSendAndWaitWithReply:
    """send_and_wait must return the reply body when a message arrives."""

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_returns_reply_text(self, MockXMPP):
        config = _make_config()
        captured_callback = None

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        async def simulate_reply():
            # Wait until the callback is registered and message is sent
            while captured_callback is None:
                await asyncio.sleep(0.01)
            # Wait a tick for the send to happen
            await asyncio.sleep(0.01)
            fake_msg = _make_slixmpp_message("user@example.com", "  yes  ")
            await captured_callback(fake_msg)

        task = asyncio.create_task(simulate_reply())
        result = await send_and_wait(config, "confirm?", timeout=5, **_FAST)
        await task

        assert result == "yes"

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_sends_message_before_waiting(self, MockXMPP):
        config = _make_config()
        captured_callback = None

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        async def simulate_reply():
            while captured_callback is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            fake_msg = _make_slixmpp_message("user@example.com", "ok")
            await captured_callback(fake_msg)

        task = asyncio.create_task(simulate_reply())
        await send_and_wait(config, "hello?", timeout=5, **_FAST)
        await task

        conn.send.assert_called_once_with("user@example.com", "hello?")

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_ignores_message_from_wrong_sender(self, MockXMPP):
        config = _make_config()
        captured_callback = None

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        async def simulate_wrong_sender():
            while captured_callback is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            # Wrong sender — should be ignored
            fake_msg = _make_slixmpp_message("stranger@example.com", "spam")
            await captured_callback(fake_msg)

        task = asyncio.create_task(simulate_wrong_sender())
        result = await send_and_wait(config, "hello?", timeout=0.2, **_FAST)
        await task

        assert result is None

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_ignores_groupchat_type(self, MockXMPP):
        config = _make_config()
        captured_callback = None

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        async def simulate_groupchat():
            while captured_callback is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            fake_msg = _make_slixmpp_message("user@example.com", "hello", mtype="groupchat")
            await captured_callback(fake_msg)

        task = asyncio.create_task(simulate_groupchat())
        result = await send_and_wait(config, "hello?", timeout=0.2, **_FAST)
        await task

        assert result is None

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_disconnects_after_reply(self, MockXMPP):
        config = _make_config()
        captured_callback = None

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        async def simulate_reply():
            while captured_callback is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            fake_msg = _make_slixmpp_message("user@example.com", "ok")
            await captured_callback(fake_msg)

        task = asyncio.create_task(simulate_reply())
        await send_and_wait(config, "hello?", timeout=5, **_FAST)
        await task

        conn.disconnect.assert_called_once()


class TestSendAndWaitTimeout:
    """send_and_wait must return None when no reply arrives before timeout."""

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_returns_none_on_timeout(self, MockXMPP):
        config = _make_config()

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None  # Register but never call
        MockXMPP.return_value = conn

        result = await send_and_wait(config, "hello?", timeout=0.1, **_FAST)

        assert result is None

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_disconnects_on_timeout(self, MockXMPP):
        config = _make_config()

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        await send_and_wait(config, "hello?", timeout=0.1, **_FAST)

        conn.disconnect.assert_called_once()

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_still_sends_message_before_timeout(self, MockXMPP):
        config = _make_config()

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        await send_and_wait(config, "are you there?", timeout=0.1, **_FAST)

        conn.send.assert_called_once_with("user@example.com", "are you there?")


class TestConnectionTimeout:
    """Connection timeout must raise ConnectionError, not raw TimeoutError."""

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_timeout_raises_connection_error(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        # Never set → will timeout
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        with pytest.raises(ConnectionError, match="XMPP connection timeout"):
            await send_and_wait(config, "hello?", timeout=5, **_FAST)

        conn.disconnect.assert_called_once()


class TestSendFailure:
    """send() returning False must raise ConnectionError instead of waiting for reply."""

    @patch("claude_xmpp_bridge.ask.XMPPConnection")
    async def test_send_failure_raises_connection_error(self, MockXMPP):
        config = _make_config()
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = False
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        with pytest.raises(ConnectionError, match="send failed"):
            await send_and_wait(config, "hello?", timeout=5, **_FAST)

        conn.disconnect.assert_called_once()
