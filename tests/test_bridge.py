"""Integration tests for bridge module — XMPPBridge orchestrator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import make_mock_conn as _make_mock_conn
from helpers import make_slixmpp_message as _make_slixmpp_message

from claude_xmpp_bridge.bridge import XMPPBridge
from claude_xmpp_bridge.config import Config


def _make_config(tmp_path: Path) -> Config:
    return Config(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
        socket_path=tmp_path / "test.sock",
        db_path=tmp_path / "test.db",
        messages_file=None,
    )


async def _socket_request(socket_path: Path, request: dict) -> dict:
    """Send a JSON request to the Unix socket and return the response."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(request).encode() + b"\n")
    writer.write_eof()
    data = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    await writer.wait_closed()
    return json.loads(data.decode())


class TestRegisterViaSocket:
    """Register a session via the Unix socket and verify it's in the registry."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_session(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "sess-1",
                    "sty": "12345.pts-0",
                    "window": "0",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )

            assert resp == {"ok": True}
            assert "sess-1" in bridge.registry.sessions
            info = bridge.registry.sessions["sess-1"]
            assert info["project"] == "/home/user/project"
            assert info["backend"] == "screen"
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_missing_session_id(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "project": "/home/user/project",
                },
            )

            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestXMPPMessageRouting:
    """Incoming XMPP messages should be routed via the multiplexer."""

    @patch("claude_xmpp_bridge.bridge.get_multiplexer")
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_plain_text_routes_to_active_session(self, MockXMPP, mock_get_mux, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)

        # Set up the mock multiplexer
        mock_mux = AsyncMock()
        mock_mux.send_text.return_value = True
        mock_get_mux.return_value = mock_mux

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # Register a session directly
        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/myproject",
            backend="screen",
        )

        # Simulate incoming XMPP message
        fake_msg = _make_slixmpp_message("user@example.com", "hello world")
        await captured["cb"](fake_msg)

        mock_get_mux.assert_called_with("screen")
        mock_mux.send_text.assert_called_once_with("12345.pts-0", "0", "hello world")

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ignores_message_from_stranger(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        XMPPBridge(config)

        # No sessions, so if it tries to route it would fail
        fake_msg = _make_slixmpp_message("stranger@example.com", "evil text")
        await captured["cb"](fake_msg)

        # Should not crash or send anything via XMPP (no session to report about)
        conn.send.assert_not_called()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ignores_groupchat(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        XMPPBridge(config)

        fake_msg = _make_slixmpp_message("user@example.com", "hello", mtype="groupchat")
        await captured["cb"](fake_msg)

        conn.send.assert_not_called()


class TestHandleCommandEdgeCases:
    """Edge cases in _handle_command: /N without arg, unknown command."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_slash_n_without_message_sends_usage(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        fake_msg = _make_slixmpp_message("user@example.com", "/1")
        await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "Usage" in conn.send.call_args[0][1]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_unknown_command_replies_with_error(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        fake_msg = _make_slixmpp_message("user@example.com", "/foobar")
        await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "Unknown command" in conn.send.call_args[0][1]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_empty_body_ignored(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        fake_msg = _make_slixmpp_message("user@example.com", "   ")
        await captured["cb"](fake_msg)
        conn.send.assert_not_called()
        bridge.registry.close()


class TestListCommand:
    """The /list command should send back a session list via XMPP."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_with_sessions(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project-a",
            backend="screen",
        )

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/list")
            await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "Sessions:" in sent_text
        assert "[⚡screen #0]" in sent_text  # window label included

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_empty(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        XMPPBridge(_make_config(tmp_path))

        fake_msg = _make_slixmpp_message("user@example.com", "/list")
        await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert sent_text == "No active sessions."

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_short_alias(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
        )

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/l")
            await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "Sessions:" in sent_text

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_shows_tmux_tag(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)
        bridge.registry.register(
            session_id="tmux-sess",
            sty="tmux-session",
            window="0",
            project="/home/user/project",
            backend="tmux",
        )

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/list")
            await captured["cb"](fake_msg)

        sent_text = conn.send.call_args[0][1]
        assert "[⚡tmux :tmux-session]" in sent_text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_shows_read_only_tag_for_no_backend(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)
        bridge.registry.register(
            session_id="ro-sess",
            sty="",
            window="",
            project="/home/user/project",
            backend=None,
        )

        fake_msg = _make_slixmpp_message("user@example.com", "/list")
        await captured["cb"](fake_msg)

        sent_text = conn.send.call_args[0][1]
        assert "[⚡read-only]" in sent_text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_shows_opencode_read_only_tag(self, MockXMPP, tmp_path):
        """OpenCode session with no backend should show [🧠read-only]."""
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)
        bridge.registry.register(
            session_id="oc-ro-sess",
            sty="",
            window="",
            project="/home/user/project",
            backend=None,
            source="opencode",
        )

        fake_msg = _make_slixmpp_message("user@example.com", "/list")
        await captured["cb"](fake_msg)

        sent_text = conn.send.call_args[0][1]
        assert "[🧠read-only]" in sent_text
        bridge.registry.close()


class TestHelpCommand:
    """The /help command should send help text via XMPP."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_help_returns_help_text(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        XMPPBridge(_make_config(tmp_path))

        fake_msg = _make_slixmpp_message("user@example.com", "/help")
        await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "/list" in sent_text
        assert "/help" in sent_text


class TestShutdownSequence:
    """Shutdown should stop socket server, send goodbye, and disconnect XMPP."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_shutdown(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        await bridge.shutdown()

        # Should have sent goodbye message
        conn.send.assert_called()
        goodbye_calls = [c for c in conn.send.call_args_list if "stopped" in str(c).lower() or "Bridge" in str(c)]
        assert len(goodbye_calls) >= 1

        # Should have disconnected
        conn.disconnect.assert_called_once()

        # Socket file should be cleaned up
        assert not config.socket_path.exists()


class TestSocketSendCommand:
    """The 'send' command via socket should forward a message over XMPP."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_via_socket(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "send",
                    "message": "notification text",
                },
            )

            assert resp == {"ok": True}
            conn.send.assert_called_once_with("user@example.com", "notification text")
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_via_socket_xmpp_down(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = False
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "send",
                    "message": "notification text",
                },
            )

            assert resp == {"ok": False}
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestUnregisterViaSocket:
    """Unregistering a session should remove it from the registry."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_unregister_session(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
        )
        assert "sess-1" in bridge.registry.sessions

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "unregister",
                    "session_id": "sess-1",
                },
            )

            assert resp == {"ok": True}
            assert "sess-1" not in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestQueryViaSocket:
    """The 'query' command should return the registered project for a session."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_query_existing_session(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/my-project",
            backend="screen",
        )

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "query",
                    "session_id": "sess-1",
                },
            )

            assert resp == {"ok": True, "project": "/home/user/my-project"}
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_query_nonexistent_session(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "query",
                    "session_id": "nonexistent",
                },
            )

            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_query_missing_session_id(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "query",
                },
            )

            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


def _mock_subprocess(returncode: int) -> AsyncMock:
    """Create a mock for asyncio.create_subprocess_exec returning given exit code."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    mock_exec = AsyncMock(return_value=proc)
    return mock_exec


class TestStaleSessionCleanup:
    """Stale session cleanup: _is_session_alive, _cleanup_stale_sessions."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_removes_dead_screen_sessions(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="dead-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project-a",
            backend="screen",
        )

        # screen -ls returns exit 1 → session is dead
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 1
        assert "dead-1" not in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_keeps_alive_sessions(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="alive-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project-a",
            backend="screen",
        )

        # screen -ls returns exit 0 → session is alive
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 0
        assert "alive-1" in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_keeps_sessions_without_backend(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="ro-1",
            sty="",
            window="",
            project="/home/user/project-a",
            backend=None,
        )

        # Should not call subprocess at all for None backend
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)) as mock_exec:
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 0
        assert "ro-1" in bridge.registry.sessions
        mock_exec.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_calls_cleanup(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="dead-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project-a",
            backend="screen",
        )

        # /list should clean up dead sessions before listing
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)):
            fake_msg = _make_slixmpp_message("user@example.com", "/list")
            await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        # After cleanup, no sessions remain → "no sessions" message
        assert sent_text == bridge.messages.no_sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_deduplicates_by_sty_window_same_session(self, MockXMPP, tmp_path):
        """Two alive sessions sharing the same sty+window — keep only newest.

        Multiple instances of the same agent in *different* windows are intentionally
        kept alive. Deduplication only fires for entries sharing the *exact same*
        terminal slot (same sty + same window number).
        """
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # Two sessions, same sty+window (e.g. ghost entry from a previous run)
        bridge.registry.register(
            session_id="ghost",
            sty="12345.pts-0",
            window="2",
            project="/home/user/project",
            backend="screen",
        )
        bridge.registry.register(
            session_id="current",
            sty="12345.pts-0",
            window="2",
            project="/home/user/project",
            backend="screen",
        )

        # Both alive — but same slot → keep only current
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 1
        assert "current" in bridge.registry.sessions
        assert "ghost" not in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_keeps_multiple_instances_same_project(self, MockXMPP, tmp_path):
        """Multiple alive sessions for same project in different windows — all kept."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # 3 instances of the same agent in 3 different windows of the same project
        bridge.registry.register(
            session_id="inst-1",
            sty="12345.pts-0",
            window="1",
            project="/home/user/project",
            backend="screen",
        )
        bridge.registry.register(
            session_id="inst-2",
            sty="12345.pts-0",
            window="2",
            project="/home/user/project",
            backend="screen",
        )
        bridge.registry.register(
            session_id="inst-3",
            sty="12345.pts-0",
            window="3",
            project="/home/user/project",
            backend="screen",
        )

        # All screen sessions alive (exit 0) — all 3 must survive
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 0
        assert "inst-1" in bridge.registry.sessions
        assert "inst-2" in bridge.registry.sessions
        assert "inst-3" in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_keeps_cross_source_same_project(self, MockXMPP, tmp_path):
        """Claude Code + OpenCode in same project, both alive — cleanup must keep both."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="cc-sess",
            sty="12345.pts-0",
            window="1",
            project="/home/user/project",
            backend="screen",
            source=None,
        )
        bridge.registry.register(
            session_id="oc-sess",
            sty="12345.pts-0",
            window="2",
            project="/home/user/project",
            backend="screen",
            source="opencode",
        )

        # Both screen sessions alive (exit 0) — cleanup must not remove either
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 0
        assert "cc-sess" in bridge.registry.sessions
        assert "oc-sess" in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_deduplicates_by_sty_window(self, MockXMPP, tmp_path):
        """Two sessions with same sty+window but different project — keep newest."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # Old stale session had window 3, then window 3 was reused by new project
        bridge.registry.register(
            session_id="stale",
            sty="5757.pts-0",
            window="3",
            project="/home/user/old-project",
            backend="screen",
        )
        bridge.registry.register(
            session_id="current",
            sty="5757.pts-0",
            window="3",
            project="/home/user/new-project",
            backend="screen",
        )

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 1
        assert "current" in bridge.registry.sessions
        assert "stale" not in bridge.registry.sessions
        bridge.registry.close()


class TestSubprocessTimeout:
    """_is_session_alive must handle subprocess timeout."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_handles_subprocess_timeout(self, MockXMPP, tmp_path):
        """Subprocess that hangs should be killed and session treated as dead."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="hanging-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project-a",
            backend="screen",
        )

        # Mock subprocess that never finishes (wait() times out), then returns after kill()
        proc = AsyncMock()
        proc.wait = AsyncMock(side_effect=[TimeoutError(), None])
        proc.kill = MagicMock()
        mock_exec = AsyncMock(return_value=proc)

        with patch("asyncio.create_subprocess_exec", mock_exec):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 1
        assert "hanging-1" not in bridge.registry.sessions
        proc.kill.assert_called_once()
        assert proc.wait.call_count >= 2  # first timeout, then cleanup wait
        bridge.registry.close()


class TestOpenCodeSourceTag:
    """OpenCode sessions should show 🧠 prefix in /list output."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_shows_brain_tag_for_opencode(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="oc-sess",
            sty="12345.pts-0",
            window="0",
            project="/home/user/my-app",
            backend="screen",
            source="opencode",
        )

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/list")
            await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "[🧠screen #0]" in sent_text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_no_brain_tag_for_claude(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        bridge.registry.register(
            session_id="cc-sess",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
            # no source → Claude Code
        )

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/list")
            await captured["cb"](fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "[⚡screen #0]" in sent_text
        assert "🧠" not in sent_text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_via_socket_stores_source(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "ses_abc123",
                    "sty": "5757.pts-0.black-arch",
                    "window": "4",
                    "project": "/home/user/claude-home",
                    "backend": "screen",
                    "source": "opencode",
                },
            )

            assert resp == {"ok": True}
            info = bridge.registry.get("ses_abc123")
            assert info is not None
            assert info["source"] == "opencode"
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestRegisterDeduplication:
    """Registering with same project or sty+window should replace old session."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_same_project_different_window_keeps_both(self, MockXMPP, tmp_path):
        """Same project but different sty+window — both sessions coexist.

        Multiple instances of the same agent running in different terminal windows
        of the same project are all welcome; deduplication only fires when the
        *exact same* multiplexer slot (sty+window) is reused.
        """
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp1 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "inst-1",
                    "sty": "11111.pts-0",
                    "window": "2",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp1 == {"ok": True}
            assert "inst-1" in bridge.registry.sessions

            # Second instance, same project, different sty+window — must NOT replace first
            resp2 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "inst-2",
                    "sty": "22222.pts-0",
                    "window": "5",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp2 == {"ok": True}
            assert "inst-2" in bridge.registry.sessions
            assert "inst-1" in bridge.registry.sessions  # both survive
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_same_sty_window_replaces_old(self, MockXMPP, tmp_path):
        """Same sty+window with different project — old session replaced."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp1 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "old-sess",
                    "sty": "12345.pts-0",
                    "window": "3",
                    "project": "/home/user/project-a",
                    "backend": "screen",
                },
            )
            assert resp1 == {"ok": True}
            assert "old-sess" in bridge.registry.sessions

            resp2 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "new-sess",
                    "sty": "12345.pts-0",
                    "window": "3",
                    "project": "/home/user/project-b",
                    "backend": "screen",
                },
            )
            assert resp2 == {"ok": True}
            assert "new-sess" in bridge.registry.sessions
            assert "old-sess" not in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_cross_source_same_project_keeps_both(self, MockXMPP, tmp_path):
        """Claude Code + OpenCode in same project — both must coexist."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            # Register Claude Code session (source=None)
            resp1 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "claude-sess",
                    "sty": "11111.pts-0",
                    "window": "1",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp1 == {"ok": True}

            # Register OpenCode session in the SAME project, different window
            resp2 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "oc-sess",
                    "sty": "11111.pts-0",
                    "window": "2",
                    "project": "/home/user/project",
                    "backend": "screen",
                    "source": "opencode",
                },
            )
            assert resp2 == {"ok": True}

            # Both must remain — cross-source sessions are not deduplicated by project
            assert "claude-sess" in bridge.registry.sessions
            assert "oc-sess" in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_different_project_different_window_keeps_both(self, MockXMPP, tmp_path):
        """Different project AND different window — both coexist."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp1 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "sess-a",
                    "sty": "12345.pts-0",
                    "window": "0",
                    "project": "/home/user/project-a",
                    "backend": "screen",
                },
            )
            assert resp1 == {"ok": True}

            resp2 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "sess-b",
                    "sty": "12345.pts-0",
                    "window": "1",
                    "project": "/home/user/project-b",
                    "backend": "screen",
                },
            )
            assert resp2 == {"ok": True}

            assert "sess-a" in bridge.registry.sessions
            assert "sess-b" in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestStableOrdering:
    """Session restarts must not change /list numbering or hijack plain-text routing."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_restart_same_window_inherits_registered_at(self, MockXMPP, tmp_path):
        """New session_id reusing the same sty+window preserves original registered_at.

        When Claude Code restarts inside the same Screen window the new session_id
        takes over the slot; we inherit the original registered_at so that /list
        numbering stays stable.
        """
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp1 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "old-sess",
                    "sty": "11111.pts-0",
                    "window": "1",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp1 == {"ok": True}
            original_time = bridge.registry.sessions["old-sess"]["registered_at"]

            # Session restarts in the SAME window (same sty + same window number)
            resp2 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "new-sess",
                    "sty": "11111.pts-0",
                    "window": "1",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp2 == {"ok": True}
            # Old session replaced because it occupied the same slot
            assert "old-sess" not in bridge.registry.sessions
            assert "new-sess" in bridge.registry.sessions
            # registered_at must be inherited from old session for stable /list ordering
            new_time = bridge.registry.sessions["new-sess"]["registered_at"]
            assert abs(new_time - original_time) < 0.001
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_restart_different_window_keeps_both(self, MockXMPP, tmp_path):
        """Session restart in a different window does NOT displace the old slot.

        If the user opens a new Screen window for the same project, both instances
        should coexist — they occupy different multiplexer slots.
        """
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp1 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "old-sess",
                    "sty": "11111.pts-0",
                    "window": "1",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp1 == {"ok": True}

            # New instance in a DIFFERENT window (same Screen session, different window#)
            resp2 = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "new-sess",
                    "sty": "11111.pts-0",
                    "window": "2",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp2 == {"ok": True}
            # Both survive — different multiplexer slots
            assert "old-sess" in bridge.registry.sessions
            assert "new-sess" in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_reregister_same_sid_does_not_change_active(self, MockXMPP, tmp_path):
        """Re-registering same session_id (e.g. OpenCode setImmediate) must not flip last_active."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            # Register two sessions
            await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "sess-a",
                    "sty": "11111.pts-0",
                    "window": "1",
                    "project": "/home/user/project-a",
                    "backend": "screen",
                },
            )
            await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "sess-b",
                    "sty": "22222.pts-0",
                    "window": "2",
                    "project": "/home/user/project-b",
                    "backend": "screen",
                },
            )
            # Explicitly set sess-a as active (user sent /1)
            bridge.registry.set_active("sess-a")
            assert bridge.registry.last_active == "sess-a"

            # sess-b re-registers (hooks fire again) — must NOT change active to sess-b
            await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "sess-b",
                    "sty": "22222.pts-0",
                    "window": "2",
                    "project": "/home/user/project-b",
                    "backend": "screen",
                },
            )
            assert bridge.registry.last_active == "sess-a"
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


# ---------------------------------------------------------------------------
# _send_to_session_by_index and _send_to_session edge cases
# ---------------------------------------------------------------------------


class TestSendToSessionEdgeCases:
    """Error paths in _send_to_session_by_index and _send_to_session."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_to_index_not_found(self, MockXMPP, tmp_path):
        """Sending to /99 with no sessions should report session not found."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        fake_msg = _make_slixmpp_message("user@example.com", "/99 hello")
        await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "not found" in conn.send.call_args[0][1].lower() or "#99" in conn.send.call_args[0][1]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_to_index_no_backend(self, MockXMPP, tmp_path):
        """Sending to a read-only session should reply with no-backend message."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="ro-sess",
            sty="",
            window="",
            project="/home/user/project",
            backend=None,
        )
        fake_msg = _make_slixmpp_message("user@example.com", "/1 hello")
        await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "multiplexer" in conn.send.call_args[0][1].lower()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_to_index_delivery_failed(self, MockXMPP, tmp_path):
        """/N send when multiplexer fails should reply with delivery-failed."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)):
            fake_msg = _make_slixmpp_message("user@example.com", "/1 hello")
            await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "failed" in conn.send.call_args[0][1].lower()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_to_index_xmpp_confirm_fails(self, MockXMPP, tmp_path):
        """Successful send but XMPP confirm fails — must not raise."""
        conn, captured = _make_mock_conn(MockXMPP)
        # send() returns False (XMPP down) to simulate confirmation failure
        conn.send.return_value = False

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/1 hello")
            await captured["cb"](fake_msg)
        # Should not raise even if XMPP send failed
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_plain_text_no_active_session(self, MockXMPP, tmp_path):
        """Plain text with no sessions should reply with no-active-session."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        fake_msg = _make_slixmpp_message("user@example.com", "hello world")
        await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "No active session" in conn.send.call_args[0][1]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_plain_text_no_backend(self, MockXMPP, tmp_path):
        """Plain text to a read-only active session replies with no-backend."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="ro-sess",
            sty="",
            window="",
            project="/home/user/project",
            backend=None,
        )
        fake_msg = _make_slixmpp_message("user@example.com", "hello world")
        await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "multiplexer" in conn.send.call_args[0][1].lower()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_plain_text_delivery_failed(self, MockXMPP, tmp_path):
        """Plain text when multiplexer fails should reply with delivery-failed."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)):
            fake_msg = _make_slixmpp_message("user@example.com", "hello world")
            await captured["cb"](fake_msg)
        conn.send.assert_called_once()
        assert "failed" in conn.send.call_args[0][1].lower()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_plain_text_xmpp_confirm_fails(self, MockXMPP, tmp_path):
        """Successful plain-text send but XMPP confirm fails — must not raise."""
        conn, captured = _make_mock_conn(MockXMPP)
        conn.send.return_value = False

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="0",
            project="/home/user/project",
            backend="screen",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "hello world")
            await captured["cb"](fake_msg)
        bridge.registry.close()


# ---------------------------------------------------------------------------
# _short_path, _is_session_alive, socket command edge cases
# ---------------------------------------------------------------------------


class TestShortPath:
    """_short_path should abbreviate home directory to ~."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_exact_home_returns_tilde(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        assert bridge._short_path(str(Path.home())) == "~"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_path_under_home_abbreviated(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        result = bridge._short_path(str(Path.home() / "projects" / "foo"))
        assert result == "~/projects/foo"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_path_outside_home_unchanged(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        result = bridge._short_path("/etc/config")
        assert result == "/etc/config"
        bridge.registry.close()


class TestIsSessionAliveEdgeCases:
    """_is_session_alive: unknown backend and missing sty."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_unknown_backend_returns_true(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="unk-sess",
            sty="12345.pts-0",
            window="0",
            project="/proj",
            backend="unknown_mux",
        )
        info = bridge.registry.get("unk-sess")
        assert info is not None
        alive = await bridge._is_session_alive(info)
        assert alive is True
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_no_sty_returns_true(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="no-sty",
            sty="",
            window="",
            project="/proj",
            backend="screen",
        )
        info = bridge.registry.get("no-sty")
        assert info is not None
        alive = await bridge._is_session_alive(info)
        assert alive is True
        bridge.registry.close()


class TestSocketCommandEdgeCases:
    """Edge cases in socket request handling."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_send_empty_message_returns_ok(self, MockXMPP, tmp_path):
        """send cmd with empty message should return ok without calling XMPP."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "send", "message": ""},
            )
            assert resp == {"ok": True}
            conn.send.assert_not_called()
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_unknown_socket_command_returns_error(self, MockXMPP, tmp_path):
        """Unknown socket command should return error dict."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "totally_unknown"},
            )
            assert "error" in resp
            assert "unknown" in resp["error"]
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_invalid_session_id_returns_error(self, MockXMPP, tmp_path):
        """Register with invalid session_id should return error (ValueError path)."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "has space",  # invalid — contains space
                    "sty": "12345.pts-0",
                    "window": "0",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_response_with_project_in_req(self, MockXMPP, tmp_path):
        """response cmd with project in payload (no session info) formats message."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "response",
                    "session_id": "nonexistent",
                    "project": "/home/user/my-project",
                    "message": "Build done",
                },
            )
            assert resp == {"ok": True}
            conn.send.assert_called_once()
            sent = conn.send.call_args[0][1]
            assert "Build done" in sent
            assert "my-project" in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_response_without_project_uses_question_mark(self, MockXMPP, tmp_path):
        """response cmd with no session and no project uses '?' as project."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "response",
                    "session_id": "nonexistent",
                    "message": "done",
                },
            )
            assert resp == {"ok": True}
            conn.send.assert_called_once()
            sent = conn.send.call_args[0][1]
            assert "[?]" in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_response_with_known_session(self, MockXMPP, tmp_path):
        """response cmd with a registered session resolves project from registry."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="known-sess",
            sty="",
            window="",
            project="/home/user/known-project",
            backend=None,
        )
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "response",
                    "session_id": "known-sess",
                    "message": "task done",
                },
            )
            assert resp == {"ok": True}
            sent = conn.send.call_args[0][1]
            assert "known-project" in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_response_empty_message_returns_ok_no_send(self, MockXMPP, tmp_path):
        """response with empty message should not call XMPP send."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "response", "session_id": "s", "message": ""},
            )
            assert resp == {"ok": True}
            conn.send.assert_not_called()
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestListIcons:
    """Consistency of ⚡ (Claude Code) and 🧠 (OpenCode) icons in /list output."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_claude_code_screen_has_lightning(self, MockXMPP, tmp_path):
        """Claude Code screen sessions should have ⚡ prefix."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="cc-1",
            sty="100.pts-0",
            window="0",
            project="/home/u/proj",
            backend="screen",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            await captured["cb"](_make_slixmpp_message("user@example.com", "/list"))
        text = conn.send.call_args[0][1]
        assert "[⚡screen #0]" in text  # window label appended
        assert "🧠" not in text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_claude_code_tmux_has_lightning(self, MockXMPP, tmp_path):
        """Claude Code tmux sessions should have ⚡ prefix."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="cc-2",
            sty="session",
            window="1",
            project="/home/u/proj",
            backend="tmux",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            await captured["cb"](_make_slixmpp_message("user@example.com", "/list"))
        text = conn.send.call_args[0][1]
        assert "[⚡tmux :session]" in text  # pane label in tmux style
        assert "🧠" not in text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_claude_code_readonly_has_lightning(self, MockXMPP, tmp_path):
        """Claude Code read-only sessions should have ⚡ prefix."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="cc-3",
            sty="",
            window="",
            project="/home/u/proj",
            backend=None,
        )
        await captured["cb"](_make_slixmpp_message("user@example.com", "/list"))
        text = conn.send.call_args[0][1]
        assert "[⚡read-only]" in text
        assert "🧠" not in text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_opencode_still_has_brain(self, MockXMPP, tmp_path):
        """OpenCode sessions must still show 🧠 prefix."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="oc-1",
            sty="200.pts-0",
            window="0",
            project="/home/u/proj",
            backend="screen",
            source="opencode",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            await captured["cb"](_make_slixmpp_message("user@example.com", "/list"))
        text = conn.send.call_args[0][1]
        assert "[🧠screen #0]" in text  # window label appended
        assert "⚡" not in text
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_mixed_sessions_both_icons(self, MockXMPP, tmp_path):
        """List with both Claude Code and OpenCode shows both icons."""
        conn, captured = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="cc-4",
            sty="300.pts-0",
            window="0",
            project="/home/u/proj-a",
            backend="screen",
        )
        bridge.registry.register(
            session_id="oc-2",
            sty="301.pts-0",
            window="0",
            project="/home/u/proj-b",
            backend="screen",
            source="opencode",
        )
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            await captured["cb"](_make_slixmpp_message("user@example.com", "/list"))
        text = conn.send.call_args[0][1]
        assert "[⚡screen #0]" in text
        assert "[🧠screen #0]" in text
        bridge.registry.close()


class TestSessionPrefix:
    """_session_prefix() and _window_label() produce correct strings."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_prefix_screen(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn
        bridge = XMPPBridge(_make_config(tmp_path))
        info = {
            "sty": "12345.pts-0",
            "window": "3",
            "project": str(Path.home() / "myproject"),
            "backend": "screen",
            "source": None,
            "registered_at": 0.0,
        }
        prefix = bridge._session_prefix(info)
        assert prefix == "⚡[~/myproject #3]"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_prefix_tmux(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn
        bridge = XMPPBridge(_make_config(tmp_path))
        info = {
            "sty": "%5",
            "window": "",
            "project": "/srv/app",
            "backend": "tmux",
            "source": None,
            "registered_at": 0.0,
        }
        prefix = bridge._session_prefix(info)
        assert prefix == "⚡[/srv/app :%5]"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_prefix_opencode(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn
        bridge = XMPPBridge(_make_config(tmp_path))
        info = {
            "sty": "12345.pts-0",
            "window": "1",
            "project": "/tmp/proj",
            "backend": "screen",
            "source": "opencode",
            "registered_at": 0.0,
        }
        prefix = bridge._session_prefix(info)
        assert prefix == "🧠[/tmp/proj #1]"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_prefix_readonly(self, MockXMPP, tmp_path):
        """Read-only session (no backend, no sty) should have no window label."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn
        bridge = XMPPBridge(_make_config(tmp_path))
        info = {
            "sty": "",
            "window": "",
            "project": "/tmp/proj",
            "backend": None,
            "source": None,
            "registered_at": 0.0,
        }
        prefix = bridge._session_prefix(info)
        assert prefix == "⚡[/tmp/proj]"
        bridge.registry.close()


class TestNotifyCommand:
    """The 'notify' socket command sends a session-prefixed XMPP message."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_notify_known_session_includes_prefix(self, MockXMPP, tmp_path):
        """notify with a registered session prepends icon + window label."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="sess-1",
            sty="12345.pts-0",
            window="2",
            project="/home/user/myproject",
            backend="screen",
        )
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "notify",
                    "session_id": "sess-1",
                    "message": "[TaskCompleted] Build done",
                },
            )
            assert resp == {"ok": True}
            conn.send.assert_called_once()
            sent = conn.send.call_args[0][1]
            assert "⚡" in sent
            assert "#2" in sent
            assert "Build done" in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_notify_unknown_session_sends_plain_message(self, MockXMPP, tmp_path):
        """notify with an unknown session_id falls back to plain message."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "notify",
                    "session_id": "nonexistent",
                    "message": "plain notification",
                },
            )
            assert resp == {"ok": True}
            conn.send.assert_called_once()
            sent = conn.send.call_args[0][1]
            assert "plain notification" in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_notify_empty_message_noop(self, MockXMPP, tmp_path):
        """notify with empty message must not call XMPP send."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "notify", "session_id": "s", "message": ""},
            )
            assert resp == {"ok": True}
            conn.send.assert_not_called()
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_notify_opencode_uses_brain_icon(self, MockXMPP, tmp_path):
        """notify for OpenCode session uses 🧠 prefix."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="oc-sess",
            sty="12345.pts-0",
            window="4",
            project="/home/user/project",
            backend="screen",
            source="opencode",
        )
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "notify",
                    "session_id": "oc-sess",
                    "message": "OpenCode task done",
                },
            )
            assert resp == {"ok": True}
            sent = conn.send.call_args[0][1]
            assert "🧠" in sent
            assert "#4" in sent
            assert "OpenCode task done" in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestMultiInstanceSameProject:
    """Multiple instances of the same agent in the same project must coexist."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_three_instances_all_registered(self, MockXMPP, tmp_path):
        """Three Claude Code sessions in three different Screen windows, same project."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            for _i, (sid, win) in enumerate([("s1", "1"), ("s2", "2"), ("s3", "3")]):
                resp = await _socket_request(
                    bridge.config.socket_path,
                    {
                        "cmd": "register",
                        "session_id": sid,
                        "sty": "12345.pts-0",
                        "window": win,
                        "project": "/home/user/project",
                        "backend": "screen",
                    },
                )
                assert resp == {"ok": True}

            assert len(bridge.registry.sessions) == 3
            for sid in ("s1", "s2", "s3"):
                assert sid in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_notify_targets_correct_window(self, MockXMPP, tmp_path):
        """notify for session in window #3 includes #3, not #1 or #2."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        for sid, win in [("s1", "1"), ("s2", "2"), ("s3", "3")]:
            bridge.registry.register(
                session_id=sid,
                sty="12345.pts-0",
                window=win,
                project="/home/user/project",
                backend="screen",
            )
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {
                    "cmd": "notify",
                    "session_id": "s3",
                    "message": "Done in window 3",
                },
            )
            assert resp == {"ok": True}
            sent = conn.send.call_args[0][1]
            assert "#3" in sent
            assert "#1" not in sent
            assert "#2" not in sent
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestListSessionsReturnsCopy:
    """registry.list_sessions() must return a copy, not a live reference."""

    def test_list_sessions_returns_copy(self, tmp_path):
        """Modifying the returned dict must not affect internal state."""
        from claude_xmpp_bridge.registry import SessionRegistry

        reg = SessionRegistry(tmp_path / "test.db")
        reg.register("s1", "sty1", "0", "/proj", backend="screen")
        snapshot = reg.list_sessions()
        snapshot["injected"] = snapshot["s1"]  # mutate copy
        assert "injected" not in reg.sessions
        reg.close()

    def test_list_sessions_reflects_current_state(self, tmp_path):
        """Subsequent calls should return updated data."""
        from claude_xmpp_bridge.registry import SessionRegistry

        reg = SessionRegistry(tmp_path / "test.db")
        reg.register("s1", "sty1", "0", "/proj", backend="screen")
        snap1 = reg.list_sessions()
        reg.register("s2", "sty2", "1", "/proj2", backend="tmux")
        snap2 = reg.list_sessions()
        assert "s2" not in snap1
        assert "s2" in snap2
        reg.close()


class TestConfigRepr:
    """Config.__repr__ must mask password."""

    def test_config_repr_masks_password(self, tmp_path):
        from claude_xmpp_bridge.config import Config

        cfg = Config(
            jid="bot@example.com",
            password="s3cr3t",
            recipient="user@example.com",
            socket_path=tmp_path / "bridge.sock",
            db_path=tmp_path / "bridge.db",
            messages_file=None,
        )
        r = repr(cfg)
        assert "s3cr3t" not in r
        assert "***" in r
        assert "bot@example.com" in r

    def test_notify_config_repr_masks_password(self):
        from claude_xmpp_bridge.config import NotifyConfig

        cfg = NotifyConfig(jid="bot@example.com", password="s3cr3t", recipient="u@x.com")
        r = repr(cfg)
        assert "s3cr3t" not in r
        assert "***" in r


class TestXMPPRepr:
    """XMPPConnection.__repr__ must not expose password."""

    def test_repr_does_not_contain_password(self):
        from claude_xmpp_bridge.xmpp import XMPPConnection

        conn = XMPPConnection("bot@example.com", "s3cr3t")
        r = repr(conn)
        assert "s3cr3t" not in r
        assert "bot@example.com" in r

    def test_password_is_private(self):
        from claude_xmpp_bridge.xmpp import XMPPConnection

        conn = XMPPConnection("bot@example.com", "s3cr3t")
        assert not hasattr(conn, "password")
        assert hasattr(conn, "_password")


class TestMessagesFrozen:
    """Messages dataclass should be immutable (frozen=True)."""

    def test_messages_is_frozen(self):
        import dataclasses

        from claude_xmpp_bridge.messages import Messages

        assert dataclasses.fields(Messages)  # has fields
        # frozen dataclass raises FrozenInstanceError on mutation
        msgs = Messages()
        try:
            msgs.bridge_started = "changed"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except dataclasses.FrozenInstanceError:
            pass


class TestSocketTokenAuth:
    """Socket token authentication — unauthorized requests must be rejected."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_request_without_token_rejected(self, MockXMPP, tmp_path):
        """When socket_token is configured, request without token is rejected."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        # Rebuild with socket_token
        from claude_xmpp_bridge.config import Config

        secure_config = Config(
            jid=config.jid,
            password=config.password,
            recipient=config.recipient,
            socket_path=config.socket_path,
            db_path=config.db_path,
            messages_file=config.messages_file,
            socket_token="mysecret",
        )
        bridge = XMPPBridge(secure_config)
        await bridge.socket_server.start()
        try:
            # Request without token
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "send", "message": "hello"},
            )
            assert resp is not None
            assert resp.get("error") == "unauthorized"
            conn.send.assert_not_called()
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_request_with_wrong_token_rejected(self, MockXMPP, tmp_path):
        """Wrong token must be rejected."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        from claude_xmpp_bridge.config import Config

        config = _make_config(tmp_path)
        secure_config = Config(
            jid=config.jid,
            password=config.password,
            recipient=config.recipient,
            socket_path=config.socket_path,
            db_path=config.db_path,
            messages_file=config.messages_file,
            socket_token="correct",
        )
        bridge = XMPPBridge(secure_config)
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "send", "message": "hello", "token": "wrong"},
            )
            assert resp is not None
            assert resp.get("error") == "unauthorized"
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_request_with_correct_token_accepted(self, MockXMPP, tmp_path):
        """Correct token must allow the request through."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        from claude_xmpp_bridge.config import Config

        config = _make_config(tmp_path)
        secure_config = Config(
            jid=config.jid,
            password=config.password,
            recipient=config.recipient,
            socket_path=config.socket_path,
            db_path=config.db_path,
            messages_file=config.messages_file,
            socket_token="correct",
        )
        bridge = XMPPBridge(secure_config)
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "send", "message": "hello", "token": "correct"},
            )
            assert resp == {"ok": True}
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_no_token_configured_allows_all(self, MockXMPP, tmp_path):
        """When no socket_token is set, all requests are allowed (backward compat)."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)  # no socket_token
        bridge = XMPPBridge(config)
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "send", "message": "hello"},
            )
            assert resp == {"ok": True}
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestSecurityLimits:
    """Security limits: MAX_SESSIONS, source whitelist, project length, XMPP body."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_max_sessions_limit(self, MockXMPP, tmp_path):
        """Registering more than MAX_SESSIONS sessions must fail."""
        from claude_xmpp_bridge.bridge import MAX_SESSIONS

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # Fill up to limit
        for i in range(MAX_SESSIONS):
            resp = bridge._handle_register(
                {
                    "session_id": f"s{i}",
                    "sty": f"sty{i}",
                    "window": "0",
                    "project": f"/proj/{i}",
                    "backend": "none",
                }
            )
            assert resp == {"ok": True}, f"Session {i} should succeed"

        # One more must fail
        resp = bridge._handle_register(
            {
                "session_id": "overflow",
                "sty": "",
                "window": "",
                "project": "/proj/overflow",
                "backend": "none",
            }
        )
        assert "error" in resp
        assert "limit" in resp["error"]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_arbitrary_source_accepted(self, MockXMPP, tmp_path):
        """Any source string is now accepted (open registry); only length is checked."""
        conn = MagicMock()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)
        resp = bridge._handle_register(
            {
                "session_id": "s1",
                "sty": "",
                "window": "",
                "project": "/proj",
                "backend": "none",
                "source": "cursor",
            }
        )
        assert resp == {"ok": True}
        assert bridge.registry.sessions["s1"]["source"] == "cursor"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_source_too_long_rejected(self, MockXMPP, tmp_path):
        """Source value longer than MAX_SOURCE_LEN must be rejected."""
        from claude_xmpp_bridge.config import MAX_SOURCE_LEN

        conn = MagicMock()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)
        resp = bridge._handle_register(
            {
                "session_id": "s1",
                "sty": "",
                "window": "",
                "project": "/proj",
                "backend": "none",
                "source": "x" * (MAX_SOURCE_LEN + 1),
            }
        )
        assert "error" in resp
        assert "too long" in resp["error"]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_project_too_long_rejected(self, MockXMPP, tmp_path):
        """Project path longer than MAX_PROJECT_LEN must be rejected."""
        from claude_xmpp_bridge.bridge import MAX_PROJECT_LEN

        conn = MagicMock()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)
        resp = bridge._handle_register(
            {
                "session_id": "s1",
                "sty": "",
                "window": "",
                "project": "/" + "a" * MAX_PROJECT_LEN,
                "backend": "none",
            }
        )
        assert "error" in resp
        assert "too long" in resp["error"]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_xmpp_body_truncated(self, MockXMPP, tmp_path):
        """XMPP message body longer than MAX_XMPP_BODY must be truncated."""
        from claude_xmpp_bridge.bridge import MAX_XMPP_BODY

        conn, captured = _make_mock_conn(MockXMPP)
        conn.send.return_value = True

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # Register a session to receive the message
        bridge.registry.register(
            session_id="s1",
            sty="sty1",
            window="0",
            project="/proj",
            backend="screen",
        )

        long_text = "A" * (MAX_XMPP_BODY + 5000)
        sent_texts: list[str] = []

        async def mock_stuff(session_id, info, text):
            sent_texts.append(text)
            return True

        bridge._stuff_to_session = mock_stuff  # type: ignore[method-assign]

        fake_msg = _make_slixmpp_message("user@example.com", long_text)
        await captured["cb"](fake_msg)

        assert sent_texts, "Expected _stuff_to_session to be called"
        assert len(sent_texts[0]) <= MAX_XMPP_BODY
        bridge.registry.close()


class TestUnexpectedSenderLogging:
    """Bridge must log a WARNING and ignore messages from unexpected senders."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_stranger_message_logs_warning(self, MockXMPP, tmp_path, caplog):
        """Message from an unexpected JID must produce a WARNING log entry."""
        import logging

        conn, captured = _make_mock_conn(MockXMPP)
        XMPPBridge(_make_config(tmp_path))

        with caplog.at_level(logging.WARNING, logger="claude_xmpp_bridge.bridge"):
            fake_msg = _make_slixmpp_message("evil@attacker.example", "exploit")
            await captured["cb"](fake_msg)

        assert any("evil@attacker.example" in r.message for r in caplog.records if r.levelno >= logging.WARNING)
        conn.send.assert_not_called()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_authorised_sender_does_not_log_warning(self, MockXMPP, tmp_path, caplog):
        """Authorised sender must not produce a sender-warning log entry."""
        import logging

        conn, captured = _make_mock_conn(MockXMPP)
        XMPPBridge(_make_config(tmp_path))

        with caplog.at_level(logging.WARNING, logger="claude_xmpp_bridge.bridge"):
            fake_msg = _make_slixmpp_message("user@example.com", "/help")
            await captured["cb"](fake_msg)

        unexpected_warn = [
            r for r in caplog.records if r.levelno >= logging.WARNING and "unexpected sender" in r.message
        ]
        assert not unexpected_warn


class TestAskTimeoutValidation:
    """_handle_ask must validate the timeout value from the socket request."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ask_non_integer_timeout_returns_error(self, MockXMPP, tmp_path):
        """Non-integer timeout value must return an error, not raise."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "ask", "message": "ok?", "timeout": "not-a-number"},
            )
            assert "error" in resp
            assert "timeout" in resp["error"]
            conn.send.assert_not_called()
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ask_zero_timeout_returns_error(self, MockXMPP, tmp_path):
        """Zero timeout must be rejected."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "ask", "message": "ok?", "timeout": 0},
            )
            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ask_negative_timeout_returns_error(self, MockXMPP, tmp_path):
        """Negative timeout must be rejected."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "ask", "message": "ok?", "timeout": -1},
            )
            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ask_over_max_timeout_returns_error(self, MockXMPP, tmp_path):
        """Timeout exceeding MAX_ASK_TIMEOUT must be rejected."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "ask", "message": "ok?", "timeout": XMPPBridge.MAX_ASK_TIMEOUT + 1},
            )
            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ask_valid_timeout_is_accepted(self, MockXMPP, tmp_path):
        """Valid timeout within range must be accepted (ask queued, times out quickly)."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                bridge.config.socket_path,
                {"cmd": "ask", "message": "ok?", "timeout": 1},  # 1s — will timeout quickly
            )
            # Should timeout with ok=False, not an error about timeout value
            assert resp.get("ok") is False
            assert resp.get("error") == "timeout"
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestRegistryValidationLimits:
    """Validation limits for STY_RE, WINDOW_RE in registry."""

    def test_sty_rejects_colon(self, tmp_path):
        """Colon in sty must be rejected (prevents tmux session:window injection)."""
        from claude_xmpp_bridge.registry import SessionRegistry

        reg = SessionRegistry(tmp_path / "test.db")
        with pytest.raises(ValueError, match="Invalid sty"):
            reg.register("s1", "session:window", "0", "/proj", backend="tmux")
        reg.close()

    def test_sty_rejects_too_long(self, tmp_path):
        """STY longer than 128 chars must be rejected."""
        from claude_xmpp_bridge.registry import SessionRegistry

        reg = SessionRegistry(tmp_path / "test.db")
        with pytest.raises(ValueError, match="Invalid sty"):
            reg.register("s1", "a" * 129, "0", "/proj")
        reg.close()

    def test_window_rejects_too_long(self, tmp_path):
        """Window longer than 6 digits must be rejected."""
        from claude_xmpp_bridge.registry import SessionRegistry

        reg = SessionRegistry(tmp_path / "test.db")
        with pytest.raises(ValueError, match="Invalid window"):
            reg.register("s1", "sty1", "1234567", "/proj")
        reg.close()

    def test_window_accepts_empty(self, tmp_path):
        """Empty window string must be accepted (screen default)."""
        from claude_xmpp_bridge.registry import SessionRegistry

        reg = SessionRegistry(tmp_path / "test.db")
        reg.register("s1", "sty1", "", "/proj")  # should not raise
        reg.close()


class TestClientTokenInjection:
    """send_to_bridge automatically injects token from env."""

    def test_token_injected_from_env(self, monkeypatch, tmp_path):
        """Token from CLAUDE_XMPP_SOCKET_TOKEN env var is added to request."""
        from claude_xmpp_bridge.client import send_to_bridge

        monkeypatch.setenv("CLAUDE_XMPP_SOCKET_TOKEN", "mytoken")

        sock_path = tmp_path / "bridge.sock"
        captured: list[dict] = []

        # Create a minimal fake socket server
        import json
        import socket as _socket
        import threading

        server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server_sock.bind(str(sock_path))
        server_sock.listen(1)
        server_sock.settimeout(2)

        def _serve():
            try:
                client, _ = server_sock.accept()
                data = client.recv(65536)
                req = json.loads(data.decode().strip())
                captured.append(req)
                client.sendall(json.dumps({"ok": True}).encode() + b"\n")
                client.close()
            except Exception:  # noqa: S110
                pass  # timeout or parse error during test teardown — ignore
            finally:
                server_sock.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        result = send_to_bridge({"cmd": "send", "message": "hello"}, sock_path)
        t.join(timeout=3)

        assert result == {"ok": True}
        assert len(captured) == 1
        assert captured[0].get("token") == "mytoken"


# ---------------------------------------------------------------------------
# Audit log assertions
# ---------------------------------------------------------------------------


def _make_config_with_audit(tmp_path: Path) -> tuple[Config, Path]:
    """Return a Config that writes audit events to a file in tmp_path."""
    from claude_xmpp_bridge.config import Config

    log_file = tmp_path / "audit.log"
    cfg = Config(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
        socket_path=tmp_path / "test.sock",
        db_path=tmp_path / "test.db",
        messages_file=None,
        audit_log=str(log_file),
    )
    return cfg, log_file


def _read_audit_log(log_file: Path) -> list[dict]:
    """Read all JSON Lines from the audit log file."""
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestAuditXmppRejected:
    """XMPP_REJECTED event must be emitted for unauthorized senders."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_stranger_message_emits_xmpp_rejected(self, MockXMPP, tmp_path):
        conn, captured = _make_mock_conn(MockXMPP)
        config, log_file = _make_config_with_audit(tmp_path)
        bridge = XMPPBridge(config)

        fake_msg = _make_slixmpp_message("evil@attacker.example", "exploit")
        await captured["cb"](fake_msg)
        bridge.audit.close()

        records = _read_audit_log(log_file)
        rejected = [r for r in records if r["event"] == "XMPP_REJECTED"]
        assert len(rejected) == 1
        assert rejected[0]["from_jid"] == "evil@attacker.example"
        bridge.registry.close()


class TestAuditTokenRejected:
    """TOKEN_REJECTED event must be emitted when socket token check fails."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_wrong_token_emits_token_rejected(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        from claude_xmpp_bridge.config import Config

        log_file = tmp_path / "audit.log"
        config = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=tmp_path / "test.sock",
            db_path=tmp_path / "test.db",
            messages_file=None,
            socket_token="correct-token",
            audit_log=str(log_file),
        )
        bridge = XMPPBridge(config)
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "send", "message": "hi", "token": "wrong-token"},
            )
            assert resp.get("error") == "unauthorized"
        finally:
            await bridge.socket_server.stop()
            bridge.audit.close()

        records = _read_audit_log(log_file)
        rejected = [r for r in records if r["event"] == "TOKEN_REJECTED"]
        assert len(rejected) == 1
        assert rejected[0]["cmd"] == "send"
        assert rejected[0]["token_provided"] is True
        bridge.registry.close()


class TestAuditAskTimeout:
    """ASK_TIMEOUT event must be emitted when ask times out."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ask_timeout_emits_audit_event(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config, log_file = _make_config_with_audit(tmp_path)
        bridge = XMPPBridge(config)
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "ask", "message": "are you there?", "timeout": 1},
            )
            assert resp.get("error") == "timeout"
        finally:
            await bridge.socket_server.stop()
            bridge.audit.close()

        records = _read_audit_log(log_file)
        timeouts = [r for r in records if r["event"] == "ASK_TIMEOUT"]
        assert len(timeouts) == 1
        assert timeouts[0]["timeout"] == 1
        bridge.registry.close()


class TestAuditSessionLifecycle:
    """SESSION_REGISTERED and SESSION_UNREGISTERED events."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_emits_session_registered(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config, log_file = _make_config_with_audit(tmp_path)
        bridge = XMPPBridge(config)
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {
                    "cmd": "register",
                    "session_id": "audit-sess",
                    "sty": "12345.pts-0",
                    "window": "1",
                    "project": "/home/user/project",
                    "backend": "screen",
                },
            )
            assert resp == {"ok": True}
        finally:
            await bridge.socket_server.stop()
            bridge.audit.close()

        records = _read_audit_log(log_file)
        registered = [r for r in records if r["event"] == "SESSION_REGISTERED"]
        assert len(registered) == 1
        assert registered[0]["session_id"] == "audit-sess"
        assert registered[0]["project"] == "/home/user/project"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_unregister_emits_session_unregistered(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config, log_file = _make_config_with_audit(tmp_path)
        bridge = XMPPBridge(config)
        bridge.registry.register(
            session_id="sess-del",
            sty="",
            window="",
            project="/home/user/del-proj",
            backend=None,
        )
        await bridge.socket_server.start()
        try:
            resp = await _socket_request(
                config.socket_path,
                {"cmd": "unregister", "session_id": "sess-del"},
            )
            assert resp == {"ok": True}
        finally:
            await bridge.socket_server.stop()
            bridge.audit.close()

        records = _read_audit_log(log_file)
        unreg = [r for r in records if r["event"] == "SESSION_UNREGISTERED"]
        assert len(unreg) == 1
        assert unreg[0]["session_id"] == "sess-del"
        assert unreg[0]["project"] == "/home/user/del-proj"
        bridge.registry.close()


class TestNoBackendSessionTTL:
    """Sessions without a backend are auto-expired after NO_BACKEND_SESSION_TTL."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_expired_no_backend_session_is_removed(self, MockXMPP, tmp_path):
        """A no-backend session older than TTL is cleaned up on /list."""
        import time

        from claude_xmpp_bridge.bridge import NO_BACKEND_SESSION_TTL

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        # Manually register a no-backend session with an old timestamp
        old_ts = time.time() - NO_BACKEND_SESSION_TTL - 10
        bridge.registry.register(
            "stale-no-backend",
            sty="",
            window="",
            project="/home/user/stale",
            backend=None,
            registered_at=old_ts,
        )
        assert "stale-no-backend" in bridge.registry.sessions

        removed = await bridge._cleanup_stale_sessions()

        assert removed >= 1
        assert "stale-no-backend" not in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_fresh_no_backend_session_is_kept(self, MockXMPP, tmp_path):
        """A no-backend session younger than TTL must NOT be removed."""
        import time

        from claude_xmpp_bridge.bridge import NO_BACKEND_SESSION_TTL

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        fresh_ts = time.time() - (NO_BACKEND_SESSION_TTL // 2)
        bridge.registry.register(
            "fresh-no-backend",
            sty="",
            window="",
            project="/home/user/fresh",
            backend=None,
            registered_at=fresh_ts,
        )

        removed = await bridge._cleanup_stale_sessions()

        assert removed == 0
        assert "fresh-no-backend" in bridge.registry.sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_expired_session_emits_audit_event(self, MockXMPP, tmp_path):
        """SESSION_EXPIRED audit event is emitted when a no-backend session expires."""
        import time

        from claude_xmpp_bridge.bridge import NO_BACKEND_SESSION_TTL

        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        log_file = tmp_path / "audit.log"
        config = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=tmp_path / "test.sock",
            db_path=tmp_path / "test.db",
            messages_file=None,
            audit_log=str(log_file),
        )
        bridge = XMPPBridge(config)
        old_ts = time.time() - NO_BACKEND_SESSION_TTL - 10
        bridge.registry.register(
            "expired-audit",
            sty="",
            window="",
            project="/home/user/expired",
            backend=None,
            registered_at=old_ts,
        )

        await bridge._cleanup_stale_sessions()
        bridge.audit.close()

        records = _read_audit_log(log_file)
        expired = [r for r in records if r["event"] == "SESSION_EXPIRED"]
        assert len(expired) == 1
        assert expired[0]["session_id"] == "expired-audit"
        assert expired[0]["reason"] == "expired"
        assert expired[0]["project"] == "/home/user/expired"
        bridge.registry.close()


# ---------------------------------------------------------------------------
# TestSourceIcons — configurable per-source icons
# ---------------------------------------------------------------------------


class TestSourceIcons:
    """_source_icon() respects DEFAULT_SOURCE_ICONS and per-config overrides."""

    def _make_bridge(self, tmp_path, source_icons=None):
        with patch("claude_xmpp_bridge.bridge.XMPPConnection") as MockXMPP:
            conn = MagicMock()
            conn.on_message.side_effect = lambda cb: None
            MockXMPP.return_value = conn
            cfg = Config(
                jid="bot@example.com",
                password="secret",
                recipient="user@example.com",
                socket_path=tmp_path / "test.sock",
                db_path=tmp_path / "test.db",
                messages_file=None,
                source_icons=source_icons or {},
            )
            return XMPPBridge(cfg)

    def test_default_none_source_is_lightning(self, tmp_path):
        bridge = self._make_bridge(tmp_path)
        assert bridge._source_icon(None) == "⚡"
        bridge.registry.close()

    def test_default_opencode_is_brain(self, tmp_path):
        bridge = self._make_bridge(tmp_path)
        assert bridge._source_icon("opencode") == "🧠"
        bridge.registry.close()

    def test_unknown_source_falls_back_to_default_icon(self, tmp_path):
        """Unknown source uses the None fallback (⚡)."""
        bridge = self._make_bridge(tmp_path)
        assert bridge._source_icon("unknownapp") == "⚡"
        bridge.registry.close()

    def test_custom_icon_overrides_default(self, tmp_path):
        """Config source_icons can override the built-in opencode icon."""
        bridge = self._make_bridge(tmp_path, source_icons={"opencode": "🤖"})
        assert bridge._source_icon("opencode") == "🤖"
        bridge.registry.close()

    def test_new_source_with_custom_icon(self, tmp_path):
        """A completely new source (e.g. cursor) gets its custom icon."""
        bridge = self._make_bridge(tmp_path, source_icons={"cursor": "🔵"})
        assert bridge._source_icon("cursor") == "🔵"
        bridge.registry.close()

    def test_custom_default_icon(self, tmp_path):
        """Config can override the fallback icon (None key)."""
        bridge = self._make_bridge(tmp_path, source_icons={None: "🟡"})
        assert bridge._source_icon(None) == "🟡"
        assert bridge._source_icon("whatever") == "🟡"
        bridge.registry.close()

    def test_session_prefix_uses_custom_icon(self, tmp_path):
        """_session_prefix() uses the icon resolved by _source_icon()."""
        bridge = self._make_bridge(tmp_path, source_icons={"cursor": "🔵"})
        proj = str(Path.home() / "proj")
        info = {
            "sty": "12345.pts-0",
            "window": "2",
            "project": proj,
            "backend": "screen",
            "source": "cursor",
            "registered_at": 0.0,
        }
        prefix = bridge._session_prefix(info)  # type: ignore[arg-type]
        assert prefix == "🔵[~/proj #2]"
        bridge.registry.close()


class TestNotifySourceHint:
    """notify falls back to source hint when session is not in registry."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_notify_unknown_session_with_source_hint_uses_icon(self, MockXMPP, tmp_path):
        """If session_id is unknown but source+project are provided, icon is correct."""
        conn = MagicMock()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        result = bridge._handle_notify(
            {
                "session_id": "nonexistent",
                "source": "opencode",
                "project": "/home/user/myproj",
                "message": "hello from opencode",
            }
        )
        assert result == {"ok": True}
        conn.send.assert_called_once()
        sent = conn.send.call_args[0][1]
        assert "🧠" in sent
        assert "hello from opencode" in sent
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_notify_unknown_session_no_hint_uses_default_icon(self, MockXMPP, tmp_path):
        """Without session or source hint, default icon (⚡) is used as prefix."""
        conn = MagicMock()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        result = bridge._handle_notify(
            {
                "session_id": "nonexistent",
                "message": "plain message",
            }
        )
        assert result == {"ok": True}
        conn.send.assert_called_once()
        sent = conn.send.call_args[0][1]
        # No project → prefix is just the default icon
        assert "⚡" in sent
        assert "plain message" in sent
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_notify_unknown_session_custom_source_icon(self, MockXMPP, tmp_path):
        """Custom source icon from config is used even for unknown session."""
        conn = MagicMock()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        cfg = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=tmp_path / "test.sock",
            db_path=tmp_path / "test.db",
            messages_file=None,
            source_icons={"cursor": "🔵"},
        )
        bridge = XMPPBridge(cfg)
        result = bridge._handle_notify(
            {
                "session_id": "nonexistent",
                "source": "cursor",
                "project": "/home/user/proj",
                "message": "cursor done",
            }
        )
        assert result == {"ok": True}
        sent = conn.send.call_args[0][1]
        assert "🔵" in sent
        assert "cursor done" in sent
        bridge.registry.close()


class TestSourceIconsConfig:
    """source_icons loaded from TOML [source_icons] section."""

    def test_source_icons_loaded_from_toml(self, monkeypatch, credentials_file, tmp_path):
        from claude_xmpp_bridge import config

        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            'jid = "bot@example.com"\n'
            'recipient = "rcpt@example.com"\n'
            "[source_icons]\n"
            'opencode = "🤖"\n'
            'cursor = "🔵"\n'
            'default = "💡"\n'
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        from claude_xmpp_bridge.config import load_config

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.source_icons["opencode"] == "🤖"
        assert cfg.source_icons["cursor"] == "🔵"
        assert cfg.source_icons[None] == "💡"

    def test_source_icons_empty_when_section_missing(self, monkeypatch, credentials_file, tmp_path):
        from claude_xmpp_bridge import config

        toml_file = tmp_path / "config.toml"
        toml_file.write_text('jid = "bot@example.com"\nrecipient = "rcpt@example.com"\n')
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        from claude_xmpp_bridge.config import load_config

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.source_icons == {}

    def test_source_icons_ignores_non_string_values(self, monkeypatch, credentials_file, tmp_path):
        from claude_xmpp_bridge import config

        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            'jid = "bot@example.com"\n'
            'recipient = "rcpt@example.com"\n'
            "[source_icons]\n"
            'opencode = "🧠"\n'
            "cursor = 42\n"  # non-string — should be ignored
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        from claude_xmpp_bridge.config import load_config

        cfg = load_config(cli_credentials=str(credentials_file))
        assert "opencode" in cfg.source_icons
        assert "cursor" not in cfg.source_icons


# ---------------------------------------------------------------------------
# TestRelay — agent-to-agent messaging
# ---------------------------------------------------------------------------


class TestRelay:
    """relay command delivers messages between agents and notifies the XMPP observer."""

    def _make_bridge_with_sessions(self, tmp_path, MockXMPP):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register(
            session_id="agent-a",
            sty="12345.pts-0",
            window="1",
            project="/home/user/project-a",
            backend="screen",
        )
        bridge.registry.register(
            session_id="agent-b",
            sty="12345.pts-0",
            window="2",
            project="/home/user/project-b",
            backend="screen",
        )
        return bridge, conn

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_by_session_id_delivers_to_target(self, MockXMPP, tmp_path):
        """relay with 'to' session_id stuffs the message into the target terminal."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            resp = await bridge._handle_relay(
                {"session_id": "agent-a", "to": "agent-b", "message": "yo agent-b, done with module X"}
            )

        assert resp == {"ok": True}
        # Observer should get an XMPP notification
        conn.send.assert_called_once()
        sent = conn.send.call_args[0][1]
        assert "↔" in sent
        assert "agent" in sent.lower() or "project" in sent.lower()
        assert "yo agent-b" in sent
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_by_index_delivers_to_target(self, MockXMPP, tmp_path):
        """relay with 'to_index' stuffs the message into the Nth session."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            resp = await bridge._handle_relay({"session_id": "agent-a", "to_index": 2, "message": "hello session 2"})

        assert resp == {"ok": True}
        conn.send.assert_called_once()
        sent = conn.send.call_args[0][1]
        assert "hello session 2" in sent
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_missing_to_and_index_returns_error(self, MockXMPP, tmp_path):
        """relay without 'to' or 'to_index' must return an error."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        resp = await bridge._handle_relay({"session_id": "agent-a", "message": "hello"})

        assert resp.get("ok") is False
        assert "relay" in resp.get("error", "").lower() or "to" in resp.get("error", "").lower()
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_missing_message_returns_error(self, MockXMPP, tmp_path):
        """relay without 'message' must return an error."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        resp = await bridge._handle_relay({"session_id": "agent-a", "to": "agent-b"})

        assert resp.get("ok") is False
        assert "message" in resp.get("error", "")
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_unknown_target_returns_error(self, MockXMPP, tmp_path):
        """relay to a non-existent session_id must return an error."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        resp = await bridge._handle_relay({"session_id": "agent-a", "to": "nonexistent", "message": "hi"})

        assert resp.get("ok") is False
        assert "not found" in resp.get("error", "").lower()
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_to_no_backend_returns_error(self, MockXMPP, tmp_path):
        """relay to a read-only session must return a descriptive error."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("sender", sty="sty1", window="1", project="/proj-a", backend="screen")
        bridge.registry.register("readonly", sty="", window="", project="/proj-ro", backend=None)

        resp = await bridge._handle_relay({"session_id": "sender", "to": "readonly", "message": "hello"})

        assert resp.get("ok") is False
        assert "multiplexer" in resp.get("error", "").lower() or "no" in resp.get("error", "").lower()
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_invalid_to_index_returns_error(self, MockXMPP, tmp_path):
        """relay with non-integer to_index must return an error."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        resp = await bridge._handle_relay({"session_id": "agent-a", "to_index": "bad", "message": "hi"})

        assert resp.get("ok") is False
        assert "integer" in resp.get("error", "").lower()
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_multiplexer_failure_returns_error(self, MockXMPP, tmp_path):
        """When the multiplexer fails, relay returns ok=False with error."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)):
            resp = await bridge._handle_relay({"session_id": "agent-a", "to": "agent-b", "message": "hello"})

        assert resp.get("ok") is False
        assert "failed" in resp.get("error", "").lower()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_via_socket(self, MockXMPP, tmp_path):
        """relay delivered via socket round-trip returns ok=True."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("ag-1", sty="sty1", window="1", project="/proj-1", backend="screen")
        bridge.registry.register("ag-2", sty="sty1", window="2", project="/proj-2", backend="screen")

        await bridge.socket_server.start()
        try:
            with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
                resp = await _socket_request(
                    bridge.config.socket_path,
                    {"cmd": "relay", "session_id": "ag-1", "to": "ag-2", "message": "coordinate!"},
                )
            assert resp == {"ok": True}
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_long_message_truncated_in_xmpp_notification(self, MockXMPP, tmp_path):
        """Observer XMPP notification truncates the message body at 200 chars."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)
        long_msg = "X" * 300

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            resp = await bridge._handle_relay({"session_id": "agent-a", "to": "agent-b", "message": long_msg})

        assert resp == {"ok": True}
        sent = conn.send.call_args[0][1]
        assert "…" in sent  # truncation marker
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_relay_without_sender_uses_fallback_label(self, MockXMPP, tmp_path):
        """relay without session_id still works and uses '?' as sender label."""
        bridge, conn = self._make_bridge_with_sessions(tmp_path, MockXMPP)

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            resp = await bridge._handle_relay({"to": "agent-b", "message": "anonymous relay"})

        assert resp == {"ok": True}
        sent = conn.send.call_args[0][1]
        assert "?" in sent
        assert "anonymous relay" in sent
        bridge.registry.close()


# ---------------------------------------------------------------------------
# TestBroadcast — one-to-all messaging
# ---------------------------------------------------------------------------


class TestBroadcast:
    """broadcast command delivers a message to all sessions except the sender."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_delivers_to_all_other_sessions(self, MockXMPP, tmp_path):
        """broadcast from agent-a reaches agent-b and agent-c, not agent-a."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")
        bridge.registry.register("agent-b", sty="sty1", window="2", project="/proj-b", backend="screen")
        bridge.registry.register("agent-c", sty="sty1", window="3", project="/proj-c", backend="screen")

        stuffed: list[tuple[str, str]] = []

        async def _mock_stuff(session_id, info, text):
            stuffed.append((session_id, text))
            return True

        bridge._stuff_to_session = _mock_stuff  # type: ignore[method-assign]

        resp = await bridge._handle_broadcast({"session_id": "agent-a", "message": "start feature X"})

        assert resp["ok"] is True
        assert resp["delivered"] == 2
        delivered_sids = [s[0] for s in stuffed]
        assert "agent-b" in delivered_sids
        assert "agent-c" in delivered_sids
        assert "agent-a" not in delivered_sids
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_xmpp_notification_sent_once(self, MockXMPP, tmp_path):
        """Observer gets exactly one XMPP summary, not one per target."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")
        bridge.registry.register("agent-b", sty="sty1", window="2", project="/proj-b", backend="screen")
        bridge.registry.register("agent-c", sty="sty1", window="3", project="/proj-c", backend="screen")

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            await bridge._handle_broadcast({"session_id": "agent-a", "message": "broadcast!"})

        assert conn.send.call_count == 1
        sent = conn.send.call_args[0][1]
        assert "📢" in sent
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_no_message_returns_error(self, MockXMPP, tmp_path):
        """broadcast without 'message' must return an error."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")

        resp = await bridge._handle_broadcast({"session_id": "agent-a"})

        assert resp.get("ok") is False
        assert "message" in resp.get("error", "")
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_no_other_sessions_returns_zero_delivered(self, MockXMPP, tmp_path):
        """broadcast when only the sender is registered returns delivered=0."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")

        resp = await bridge._handle_broadcast({"session_id": "agent-a", "message": "lonely"})

        assert resp["ok"] is True
        assert resp["delivered"] == 0
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_skips_no_backend_sessions(self, MockXMPP, tmp_path):
        """Read-only sessions (no backend) are skipped by broadcast."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")
        bridge.registry.register("ro-sess", sty="", window="", project="/proj-ro", backend=None)

        resp = await bridge._handle_broadcast({"session_id": "agent-a", "message": "hello"})

        # ro-sess has no backend → skipped → delivered=0
        assert resp["ok"] is True
        assert resp["delivered"] == 0
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_partial_failure_reported(self, MockXMPP, tmp_path):
        """broadcast tracks delivered and failed counts separately."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")
        bridge.registry.register("agent-b", sty="sty1", window="2", project="/proj-b", backend="screen")
        bridge.registry.register("agent-c", sty="sty1", window="3", project="/proj-c", backend="screen")

        call_count = [0]

        async def _partial_stuff(session_id, info, text):
            call_count[0] += 1
            # First target succeeds, second fails
            return call_count[0] == 1

        bridge._stuff_to_session = _partial_stuff  # type: ignore[method-assign]

        resp = await bridge._handle_broadcast({"session_id": "agent-a", "message": "test"})

        assert resp["ok"] is True
        assert resp["delivered"] == 1
        assert resp["failed"] == 1
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_via_socket(self, MockXMPP, tmp_path):
        """broadcast delivered via socket round-trip returns ok=True and delivered count."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("ag-1", sty="sty1", window="1", project="/proj-1", backend="screen")
        bridge.registry.register("ag-2", sty="sty1", window="2", project="/proj-2", backend="screen")

        await bridge.socket_server.start()
        try:
            with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
                resp = await _socket_request(
                    bridge.config.socket_path,
                    {"cmd": "broadcast", "session_id": "ag-1", "message": "all agents stand by"},
                )
            assert resp.get("ok") is True
            assert resp.get("delivered") == 1
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_broadcast_long_message_truncated_in_xmpp(self, MockXMPP, tmp_path):
        """Observer XMPP notification truncates message at 200 chars."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.send.return_value = True
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        bridge = XMPPBridge(_make_config(tmp_path))
        bridge.registry.register("agent-a", sty="sty1", window="1", project="/proj-a", backend="screen")
        bridge.registry.register("agent-b", sty="sty1", window="2", project="/proj-b", backend="screen")

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            await bridge._handle_broadcast({"session_id": "agent-a", "message": "Y" * 300})

        sent = conn.send.call_args[0][1]
        assert "…" in sent
        bridge.registry.close()
