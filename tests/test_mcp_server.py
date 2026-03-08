"""Tests for BridgeMCPServer — unit tests that exercise tool implementations
without starting the actual HTTP server.

All tests interact with the tool implementation methods directly
(``_tool_send_message``, ``_tool_receive_messages``, etc.) so no network I/O
is required.  The ``XMPPBridge`` dependency is fully mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_xmpp_bridge.mcp_server import MAX_QUEUE_SIZE, BridgeMCPServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_session_info(
    project: str = "/home/user/myproject",
    backend: str | None = "screen",
    sty: str = "12345.pts-0.host",
    window: str = "1",
    source: str = "opencode",
) -> dict:
    return {
        "project": project,
        "backend": backend,
        "sty": sty,
        "window": window,
        "source": source,
        "registered_at": 1_000_000.0,
    }


def _make_bridge(sessions: dict | None = None) -> MagicMock:
    """Create a minimal XMPPBridge mock."""
    bridge = MagicMock()
    bridge.registry = MagicMock()
    bridge.registry.sessions = sessions or {}
    bridge.registry.get = MagicMock(side_effect=lambda sid: (sessions or {}).get(sid))
    bridge._stuff_to_session = AsyncMock(return_value=True)
    bridge._xmpp_send = MagicMock(return_value=True)
    bridge._session_prefix = MagicMock(side_effect=lambda info: f"[{info['project'].split('/')[-1]}]")
    bridge.audit = MagicMock()
    bridge.messages = MagicMock()
    bridge.messages.mcp_send_missing_to = "send_message requires 'to' (session_id)"
    bridge.messages.mcp_send_missing_message = "send_message requires 'message'"
    bridge.messages.mcp_send_target_not_found = "Target session not found: {to}"
    bridge.messages.mcp_send_no_backend = "Target session [{project}] has no multiplexer"
    bridge.messages.mcp_send_failed = "Delivery to [{project}] failed"
    bridge.messages.mcp_send_ok = "Message delivered to {target_prefix}"
    bridge.messages.broadcast_no_message = "broadcast requires 'message'"
    bridge.messages.broadcast_sent = "broadcast → {count} session(s)"
    return bridge


@pytest.fixture
def server() -> BridgeMCPServer:
    return BridgeMCPServer(port=17878)


@pytest.fixture
def bridge() -> MagicMock:
    sessions = {
        "ses_AAA": _make_session_info(project="/home/user/alpha"),
        "ses_BBB": _make_session_info(project="/home/user/beta"),
    }
    return _make_bridge(sessions)


@pytest.fixture
async def started_server(server: BridgeMCPServer, bridge: MagicMock) -> BridgeMCPServer:
    """Server with bridge attached but HTTP task NOT started (avoid real network)."""
    server._bridge = bridge
    return server


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


class TestBridgeMCPServerInit:
    def test_port_stored(self):
        srv = BridgeMCPServer(port=9999)
        assert srv.port == 9999

    def test_bridge_initially_none(self, server: BridgeMCPServer):
        assert server._bridge is None

    def test_queues_initially_empty(self, server: BridgeMCPServer):
        assert len(server._queues) == 0

    def test_task_initially_none(self, server: BridgeMCPServer):
        assert server._task is None

    def test_mcp_initially_none(self, server: BridgeMCPServer):
        assert server._mcp is None


# ---------------------------------------------------------------------------
# enqueue / receive
# ---------------------------------------------------------------------------


class TestEnqueueAndReceive:
    def test_enqueue_single_message(self, server: BridgeMCPServer):
        server.enqueue("ses_AAA", "hello")
        msgs = server._tool_receive_messages(session_id="ses_AAA")
        assert msgs == ["hello"]

    def test_receive_drains_queue(self, server: BridgeMCPServer):
        server.enqueue("ses_AAA", "msg1")
        server.enqueue("ses_AAA", "msg2")
        msgs = server._tool_receive_messages(session_id="ses_AAA")
        assert msgs == ["msg1", "msg2"]
        # Queue should be empty now
        assert server._tool_receive_messages(session_id="ses_AAA") == []

    def test_receive_unknown_session_returns_empty(self, server: BridgeMCPServer):
        assert server._tool_receive_messages(session_id="ses_UNKNOWN") == []

    def test_queues_are_per_session(self, server: BridgeMCPServer):
        server.enqueue("ses_AAA", "for_A")
        server.enqueue("ses_BBB", "for_B")
        assert server._tool_receive_messages(session_id="ses_AAA") == ["for_A"]
        assert server._tool_receive_messages(session_id="ses_BBB") == ["for_B"]

    def test_queue_overflow_drops_oldest(self, server: BridgeMCPServer):
        for i in range(MAX_QUEUE_SIZE + 5):
            server.enqueue("ses_AAA", f"msg_{i}")
        msgs = server._tool_receive_messages(session_id="ses_AAA")
        # Should have exactly MAX_QUEUE_SIZE messages
        assert len(msgs) == MAX_QUEUE_SIZE
        # Oldest should have been dropped — newest should be last
        assert msgs[-1] == f"msg_{MAX_QUEUE_SIZE + 4}"

    def test_receive_after_bridge_not_set_returns_empty(self, server: BridgeMCPServer):
        # No bridge needed — _tool_receive_messages works independently
        assert server._tool_receive_messages(session_id="ses_X") == []

    def test_receive_logs_audit_when_messages_present(self, started_server: BridgeMCPServer):
        started_server.enqueue("ses_AAA", "hello")
        started_server._tool_receive_messages(session_id="ses_AAA")
        started_server._bridge.audit.log.assert_called()
        event_arg = started_server._bridge.audit.log.call_args[0][0]
        assert event_arg == "MCP_RECEIVE"

    def test_receive_no_audit_when_empty(self, started_server: BridgeMCPServer):
        # No messages — audit should NOT be called
        started_server._tool_receive_messages(session_id="ses_AAA")
        started_server._bridge.audit.log.assert_not_called()


# ---------------------------------------------------------------------------
# send_message tool
# ---------------------------------------------------------------------------


class TestSendMessageTool:
    async def test_send_success(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="ping")
        assert "delivered" in result.lower() or "alpha" in result.lower()
        started_server._bridge._stuff_to_session.assert_awaited_once()

    async def test_send_enqueues_for_mcp(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(to="ses_AAA", message="ping")
        msgs = started_server._tool_receive_messages(session_id="ses_AAA")
        assert msgs == ["ping"]

    async def test_send_missing_to(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="", message="hello")
        assert "requires" in result.lower() or "to" in result.lower()

    async def test_send_missing_message(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="")
        assert "requires" in result.lower() or "message" in result.lower()

    async def test_send_target_not_found(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_NONEXISTENT", message="hello")
        assert "not found" in result.lower()

    async def test_send_no_backend(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.sessions["ses_NOBACK"] = _make_session_info(
            project="/home/user/noback", backend=None
        )
        started_server._bridge.registry.get = MagicMock(
            side_effect=lambda sid: started_server._bridge.registry.sessions.get(sid)
        )
        result = await started_server._tool_send_message(to="ses_NOBACK", message="hello")
        assert "multiplexer" in result.lower() or "no backend" in result.lower() or "noback" in result.lower()

    async def test_send_delivery_failure(self, started_server: BridgeMCPServer):
        started_server._bridge._stuff_to_session = AsyncMock(return_value=False)
        result = await started_server._tool_send_message(to="ses_AAA", message="ping")
        assert "failed" in result.lower() or "delivery" in result.lower()

    async def test_send_notifies_xmpp(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(to="ses_AAA", message="ping")
        started_server._bridge._xmpp_send.assert_called_once()
        call_arg = started_server._bridge._xmpp_send.call_args[0][0]
        assert "[MCP:" in call_arg

    async def test_send_returns_message_id(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="ping")
        assert "[id:" in result

    async def test_send_screen_false_skips_relay(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="ping", screen=False)
        started_server._bridge._stuff_to_session.assert_not_awaited()
        assert "inbox only" in result

    async def test_send_screen_false_enqueues(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(to="ses_AAA", message="ping", screen=False)
        msgs = started_server._tool_receive_messages(session_id="ses_AAA")
        assert msgs == ["ping"]

    async def test_send_screen_false_no_backend_ok(self, started_server: BridgeMCPServer):
        """screen=False should succeed even for sessions without a backend."""
        started_server._bridge.registry.sessions["ses_NOBACK"] = _make_session_info(
            project="/home/user/noback", backend=None
        )
        started_server._bridge.registry.get = MagicMock(
            side_effect=lambda sid: started_server._bridge.registry.sessions.get(sid)
        )
        result = await started_server._tool_send_message(to="ses_NOBACK", message="ping", screen=False)
        assert "inbox only" in result
        started_server._bridge._stuff_to_session.assert_not_awaited()

    async def test_send_logs_audit(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(to="ses_AAA", message="ping")
        started_server._bridge.audit.log.assert_called()
        event_arg = started_server._bridge.audit.log.call_args[0][0]
        assert event_arg == "MCP_SEND"

    async def test_bridge_not_set_returns_error(self, server: BridgeMCPServer):
        result = await server._tool_send_message(to="ses_AAA", message="hello")
        assert "bridge not initialised" in result.lower() or "error" in result.lower()


# ---------------------------------------------------------------------------
# broadcast_message tool
# ---------------------------------------------------------------------------


class TestBroadcastMessageTool:
    async def test_broadcast_to_all(self, started_server: BridgeMCPServer):
        result = await started_server._tool_broadcast_message(message="hello all", sender_session_id="")
        assert "2" in result  # 2 sessions delivered
        assert started_server._bridge._stuff_to_session.await_count == 2

    async def test_broadcast_excludes_sender(self, started_server: BridgeMCPServer):
        result = await started_server._tool_broadcast_message(message="hello all", sender_session_id="ses_AAA")
        assert "1" in result  # only ses_BBB
        assert started_server._bridge._stuff_to_session.await_count == 1

    async def test_broadcast_enqueues_for_mcp(self, started_server: BridgeMCPServer):
        await started_server._tool_broadcast_message(message="broadcast msg", sender_session_id="ses_AAA")
        # ses_BBB should have message in its MCP queue
        msgs = started_server._tool_receive_messages(session_id="ses_BBB")
        assert msgs == ["broadcast msg"]

    async def test_broadcast_missing_message(self, started_server: BridgeMCPServer):
        result = await started_server._tool_broadcast_message(message="", sender_session_id="")
        assert "requires" in result.lower() or "message" in result.lower()

    async def test_broadcast_no_sessions(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.sessions = {}
        result = await started_server._tool_broadcast_message(message="hello", sender_session_id="")
        assert "0" in result

    async def test_broadcast_notifies_xmpp(self, started_server: BridgeMCPServer):
        await started_server._tool_broadcast_message(message="hi", sender_session_id="")
        started_server._bridge._xmpp_send.assert_called_once()
        call_arg = started_server._bridge._xmpp_send.call_args[0][0]
        assert "[MCP]" in call_arg

    async def test_broadcast_logs_audit(self, started_server: BridgeMCPServer):
        await started_server._tool_broadcast_message(message="hi", sender_session_id="")
        started_server._bridge.audit.log.assert_called()
        event_arg = started_server._bridge.audit.log.call_args[0][0]
        assert event_arg == "MCP_BROADCAST"

    async def test_bridge_not_set_returns_error(self, server: BridgeMCPServer):
        result = await server._tool_broadcast_message(message="hi", sender_session_id="")
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# list_sessions tool
# ---------------------------------------------------------------------------


class TestListSessionsTool:
    def test_list_returns_all_sessions(self, started_server: BridgeMCPServer):
        result = started_server._tool_list_sessions()
        assert len(result) == 2
        ids = {s["session_id"] for s in result}
        assert ids == {"ses_AAA", "ses_BBB"}

    def test_list_session_fields(self, started_server: BridgeMCPServer):
        result = started_server._tool_list_sessions()
        for s in result:
            assert "session_id" in s
            assert "project" in s
            assert "backend" in s
            assert "source" in s
            assert "window" in s

    def test_list_empty_when_no_sessions(self, server: BridgeMCPServer):
        server._bridge = _make_bridge(sessions={})
        result = server._tool_list_sessions()
        assert result == []

    def test_list_without_bridge(self, server: BridgeMCPServer):
        result = server._tool_list_sessions()
        assert result == []

    def test_list_backend_null_for_no_backend(self, server: BridgeMCPServer):
        sessions = {"ses_X": _make_session_info(backend=None)}
        server._bridge = _make_bridge(sessions=sessions)
        result = server._tool_list_sessions()
        assert result[0]["backend"] == "null"


# ---------------------------------------------------------------------------
# _build_mcp — verifies tool registration
# ---------------------------------------------------------------------------


class TestBuildMcp:
    def test_build_mcp_creates_fastmcp_instance(self, started_server: BridgeMCPServer):
        mcp = started_server._build_mcp()
        assert mcp is not None

    def test_build_mcp_registers_tools(self, started_server: BridgeMCPServer):
        mcp = started_server._build_mcp()
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "send_message" in tool_names
        assert "receive_messages" in tool_names
        assert "broadcast_message" in tool_names
        assert "list_sessions" in tool_names

    def test_build_mcp_uses_correct_port(self, started_server: BridgeMCPServer):
        mcp = started_server._build_mcp()
        assert mcp.settings.port == started_server.port


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


class TestStartStop:
    async def test_start_sets_bridge(self, server: BridgeMCPServer, bridge: MagicMock):
        with patch.object(server, "_serve", new_callable=AsyncMock):
            await server.start(bridge)
            assert server._bridge is bridge

    async def test_start_creates_task(self, server: BridgeMCPServer, bridge: MagicMock):
        with patch.object(server, "_serve", new_callable=AsyncMock):
            await server.start(bridge)
            assert server._task is not None

    async def test_stop_cancels_task(self, server: BridgeMCPServer, bridge: MagicMock):
        async def _forever() -> None:
            await asyncio.sleep(9999)

        with patch.object(server, "_serve", side_effect=_forever):
            await server.start(bridge)
            assert server._task is not None
            await server.stop()
            assert server._task.done()

    async def test_stop_with_no_task_is_safe(self, server: BridgeMCPServer):
        # Should not raise
        await server.stop()


# ---------------------------------------------------------------------------
# _short_path
# ---------------------------------------------------------------------------


class TestShortPath:
    def test_home_replaced(self, server: BridgeMCPServer):
        import os

        home = os.path.expanduser("~")
        result = server._short_path(f"{home}/myproject")
        assert result == "~/myproject"

    def test_non_home_unchanged(self, server: BridgeMCPServer):
        result = server._short_path("/tmp/other")
        assert result == "/tmp/other"


# ---------------------------------------------------------------------------
# Config integration — mcp_port
# ---------------------------------------------------------------------------


class TestMcpPortConfig:
    def test_mcp_port_default(self):
        from claude_xmpp_bridge.config import DEFAULT_MCP_PORT

        assert DEFAULT_MCP_PORT == 7878

    def test_mcp_server_disabled_when_port_zero(self):
        """XMPPBridge should set mcp_server=None when mcp_port=0."""
        from unittest.mock import patch

        from claude_xmpp_bridge.bridge import XMPPBridge
        from claude_xmpp_bridge.config import Config

        cfg = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=__import__("pathlib").Path("/tmp/test.sock"),
            db_path=__import__("pathlib").Path("/tmp/test.db"),
            messages_file=None,
            mcp_port=0,
        )
        with (
            patch("claude_xmpp_bridge.bridge.XMPPConnection"),
            patch("claude_xmpp_bridge.bridge.SocketServer"),
            patch("claude_xmpp_bridge.bridge.SessionRegistry"),
            patch("claude_xmpp_bridge.bridge.AuditLogger"),
        ):
            bridge = XMPPBridge(cfg)
            assert bridge.mcp_server is None

    def test_mcp_server_created_when_port_nonzero(self):
        """XMPPBridge should create BridgeMCPServer when mcp_port > 0."""
        from unittest.mock import patch

        from claude_xmpp_bridge.bridge import XMPPBridge
        from claude_xmpp_bridge.config import Config

        cfg = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=__import__("pathlib").Path("/tmp/test.sock"),
            db_path=__import__("pathlib").Path("/tmp/test.db"),
            messages_file=None,
            mcp_port=7878,
        )
        with (
            patch("claude_xmpp_bridge.bridge.XMPPConnection"),
            patch("claude_xmpp_bridge.bridge.SocketServer"),
            patch("claude_xmpp_bridge.bridge.SessionRegistry"),
            patch("claude_xmpp_bridge.bridge.AuditLogger"),
        ):
            bridge = XMPPBridge(cfg)
            assert bridge.mcp_server is not None
            assert bridge.mcp_server.port == 7878


# ---------------------------------------------------------------------------
# Bridge integration — _enqueue_for_mcp
# ---------------------------------------------------------------------------


class TestEnqueueForMcp:
    def test_enqueue_for_mcp_calls_server_enqueue(self):
        from unittest.mock import MagicMock, patch

        from claude_xmpp_bridge.bridge import XMPPBridge
        from claude_xmpp_bridge.config import Config

        cfg = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=__import__("pathlib").Path("/tmp/test.sock"),
            db_path=__import__("pathlib").Path("/tmp/test.db"),
            messages_file=None,
            mcp_port=7878,
        )
        with (
            patch("claude_xmpp_bridge.bridge.XMPPConnection"),
            patch("claude_xmpp_bridge.bridge.SocketServer"),
            patch("claude_xmpp_bridge.bridge.SessionRegistry"),
            patch("claude_xmpp_bridge.bridge.AuditLogger"),
        ):
            bridge = XMPPBridge(cfg)
            assert bridge.mcp_server is not None
            bridge.mcp_server.enqueue = MagicMock()
            bridge._enqueue_for_mcp("ses_TEST", "hello")
            bridge.mcp_server.enqueue.assert_called_once_with("ses_TEST", "hello")

    def test_enqueue_for_mcp_noop_when_disabled(self):
        from unittest.mock import patch

        from claude_xmpp_bridge.bridge import XMPPBridge
        from claude_xmpp_bridge.config import Config

        cfg = Config(
            jid="bot@example.com",
            password="secret",
            recipient="user@example.com",
            socket_path=__import__("pathlib").Path("/tmp/test.sock"),
            db_path=__import__("pathlib").Path("/tmp/test.db"),
            messages_file=None,
            mcp_port=0,
        )
        with (
            patch("claude_xmpp_bridge.bridge.XMPPConnection"),
            patch("claude_xmpp_bridge.bridge.SocketServer"),
            patch("claude_xmpp_bridge.bridge.SessionRegistry"),
            patch("claude_xmpp_bridge.bridge.AuditLogger"),
        ):
            bridge = XMPPBridge(cfg)
            # Should not raise even though mcp_server is None
            bridge._enqueue_for_mcp("ses_TEST", "hello")
