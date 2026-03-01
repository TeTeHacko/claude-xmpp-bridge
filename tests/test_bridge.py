"""Integration tests for bridge module — XMPPBridge orchestrator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_slixmpp_message(from_bare: str, body: str, mtype: str = "chat") -> MagicMock:
    """Create a fake slixmpp Message object."""
    msg = MagicMock()
    msg.__getitem__ = lambda self, key: {
        "type": mtype,
        "from": MagicMock(bare=from_bare),
        "body": body,
    }[key]
    return msg


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
            resp = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "sess-1",
                "sty": "12345.pts-0",
                "window": "0",
                "project": "/home/user/project",
                "backend": "screen",
            })

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
            resp = await _socket_request(config.socket_path, {
                "cmd": "register",
                "project": "/home/user/project",
            })

            assert "error" in resp
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()


class TestXMPPMessageRouting:
    """Incoming XMPP messages should be routed via the multiplexer."""

    @patch("claude_xmpp_bridge.bridge.get_multiplexer")
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_plain_text_routes_to_active_session(self, MockXMPP, mock_get_mux, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

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
        assert captured_callback is not None
        await captured_callback(fake_msg)

        mock_get_mux.assert_called_with("screen")
        mock_mux.send_text.assert_called_once_with("12345.pts-0", "0", "hello world")

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ignores_message_from_stranger(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        XMPPBridge(config)

        # No sessions, so if it tries to route it would fail
        fake_msg = _make_slixmpp_message("stranger@example.com", "evil text")
        assert captured_callback is not None
        await captured_callback(fake_msg)

        # Should not crash or send anything via XMPP (no session to report about)
        conn.send.assert_not_called()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_ignores_groupchat(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        XMPPBridge(config)

        fake_msg = _make_slixmpp_message("user@example.com", "hello", mtype="groupchat")
        assert captured_callback is not None
        await captured_callback(fake_msg)

        conn.send.assert_not_called()


class TestListCommand:
    """The /list command should send back a session list via XMPP."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_with_sessions(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

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
            assert captured_callback is not None
            await captured_callback(fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "Claude sessions:" in sent_text
        assert "[screen]" in sent_text

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_empty(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        XMPPBridge(config)

        fake_msg = _make_slixmpp_message("user@example.com", "/list")
        assert captured_callback is not None
        await captured_callback(fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert sent_text == "No active Claude sessions."

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_list_short_alias(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
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

        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            fake_msg = _make_slixmpp_message("user@example.com", "/l")
            assert captured_callback is not None
            await captured_callback(fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        assert "Claude sessions:" in sent_text


class TestHelpCommand:
    """The /help command should send help text via XMPP."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_help_returns_help_text(self, MockXMPP, tmp_path):
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        XMPPBridge(config)

        fake_msg = _make_slixmpp_message("user@example.com", "/help")
        assert captured_callback is not None
        await captured_callback(fake_msg)

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
        goodbye_calls = [
            c for c in conn.send.call_args_list
            if "stopped" in str(c).lower() or "Bridge" in str(c)
        ]
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
            resp = await _socket_request(config.socket_path, {
                "cmd": "send",
                "message": "notification text",
            })

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
            resp = await _socket_request(config.socket_path, {
                "cmd": "send",
                "message": "notification text",
            })

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
            resp = await _socket_request(config.socket_path, {
                "cmd": "unregister",
                "session_id": "sess-1",
            })

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
            resp = await _socket_request(config.socket_path, {
                "cmd": "query",
                "session_id": "sess-1",
            })

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
            resp = await _socket_request(config.socket_path, {
                "cmd": "query",
                "session_id": "nonexistent",
            })

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
            resp = await _socket_request(config.socket_path, {
                "cmd": "query",
            })

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
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        captured_callback = None

        def capture_on_message(cb):
            nonlocal captured_callback
            captured_callback = cb

        conn.on_message.side_effect = capture_on_message
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

        # /list should clean up dead sessions before listing
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(1)):
            fake_msg = _make_slixmpp_message("user@example.com", "/list")
            assert captured_callback is not None
            await captured_callback(fake_msg)

        conn.send.assert_called_once()
        sent_text = conn.send.call_args[0][1]
        # After cleanup, no sessions remain → "no sessions" message
        assert sent_text == bridge.messages.no_sessions
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_cleanup_deduplicates_by_project(self, MockXMPP, tmp_path):
        """Multiple alive sessions for same project — keep only newest."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        # Register 3 sessions for same project (simulating stale duplicates)
        bridge.registry.register(
            session_id="oldest",
            sty="12345.pts-0",
            window="1",
            project="/home/user/project",
            backend="screen",
        )
        bridge.registry.register(
            session_id="middle",
            sty="12345.pts-0",
            window="2",
            project="/home/user/project",
            backend="screen",
        )
        bridge.registry.register(
            session_id="newest",
            sty="12345.pts-0",
            window="3",
            project="/home/user/project",
            backend="screen",
        )

        # All screen sessions alive (exit 0) — but duplicates should be removed
        with patch("asyncio.create_subprocess_exec", _mock_subprocess(0)):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 2
        assert "newest" in bridge.registry.sessions
        assert "oldest" not in bridge.registry.sessions
        assert "middle" not in bridge.registry.sessions
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

        # Mock subprocess that never finishes (wait() times out)
        proc = AsyncMock()
        proc.wait = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock()
        mock_exec = AsyncMock(return_value=proc)

        with patch("asyncio.create_subprocess_exec", mock_exec):
            removed = await bridge._cleanup_stale_sessions()

        assert removed == 1
        assert "hanging-1" not in bridge.registry.sessions
        proc.kill.assert_called_once()
        bridge.registry.close()


class TestRegisterDeduplication:
    """Registering with same project or sty+window should replace old session."""

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_register_same_project_replaces_old(self, MockXMPP, tmp_path):
        """Same project with different sty — old session replaced."""
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        bridge = XMPPBridge(config)

        await bridge.socket_server.start()
        try:
            resp1 = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "old-sess",
                "sty": "11111.pts-0",
                "window": "2",
                "project": "/home/user/project",
                "backend": "screen",
            })
            assert resp1 == {"ok": True}
            assert "old-sess" in bridge.registry.sessions

            # New session, same project, different sty+window
            resp2 = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "new-sess",
                "sty": "22222.pts-0",
                "window": "5",
                "project": "/home/user/project",
                "backend": "screen",
            })
            assert resp2 == {"ok": True}
            assert "new-sess" in bridge.registry.sessions
            assert "old-sess" not in bridge.registry.sessions
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
            resp1 = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "old-sess",
                "sty": "12345.pts-0",
                "window": "3",
                "project": "/home/user/project-a",
                "backend": "screen",
            })
            assert resp1 == {"ok": True}
            assert "old-sess" in bridge.registry.sessions

            resp2 = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "new-sess",
                "sty": "12345.pts-0",
                "window": "3",
                "project": "/home/user/project-b",
                "backend": "screen",
            })
            assert resp2 == {"ok": True}
            assert "new-sess" in bridge.registry.sessions
            assert "old-sess" not in bridge.registry.sessions
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
            resp1 = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "sess-a",
                "sty": "12345.pts-0",
                "window": "0",
                "project": "/home/user/project-a",
                "backend": "screen",
            })
            assert resp1 == {"ok": True}

            resp2 = await _socket_request(config.socket_path, {
                "cmd": "register",
                "session_id": "sess-b",
                "sty": "12345.pts-0",
                "window": "1",
                "project": "/home/user/project-b",
                "backend": "screen",
            })
            assert resp2 == {"ok": True}

            assert "sess-a" in bridge.registry.sessions
            assert "sess-b" in bridge.registry.sessions
        finally:
            await bridge.socket_server.stop()
            bridge.registry.close()
