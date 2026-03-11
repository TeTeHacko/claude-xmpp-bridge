"""Tests for BridgeMCPServer — unit tests that exercise tool implementations
without starting the actual HTTP server.

All tests interact with the tool implementation methods directly
(``_tool_send_message``, ``_tool_receive_messages``, etc.) so no network I/O
is required.  The ``XMPPBridge`` dependency is fully mocked.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_xmpp_bridge.mcp_server import BridgeMCPServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_session_info(
    project: str = "/home/user/myproject",
    backend: str | None = "screen",
    sty: str = "12345.pts-0.host",
    window: str = "1",
    source: str = "opencode",
    plugin_version: str | None = None,
    agent_state: str | None = None,
    agent_mode: str | None = None,
    todos_version: int = 0,
) -> dict:
    return {
        "project": project,
        "backend": backend,
        "sty": sty,
        "window": window,
        "source": source,
        "registered_at": 1_000_000.0,
        "plugin_version": plugin_version,
        "agent_state": agent_state,
        "agent_mode": agent_mode,
        "todos_version": todos_version,
    }


def _make_bridge(sessions: dict | None = None) -> MagicMock:
    """Create a minimal XMPPBridge mock."""
    bridge = MagicMock()
    bridge.registry = MagicMock()
    bridge.registry.sessions = sessions or {}
    bridge.registry.get = MagicMock(side_effect=lambda sid: (sessions or {}).get(sid))
    file_locks: dict[str, dict] = {}
    todos_by_session: dict[str, list[dict]] = {}
    last_agent_sender_by_session: dict[str, str | None] = {}

    def _acquire_file_lock(session_id: str, filepath: str, project: str, reason: str | None = None):
        existing = file_locks.get(filepath)
        if existing is None:
            lock = {
                "session_id": session_id,
                "filepath": filepath,
                "project": project,
                "reason": reason,
                "locked_at": "2026-03-11T01:00:00+01:00",
            }
            file_locks[filepath] = lock
            return True, lock, False
        if existing["session_id"] == session_id or existing["session_id"] not in bridge.registry.sessions:
            lock = {
                "session_id": session_id,
                "filepath": filepath,
                "project": project,
                "reason": reason,
                "locked_at": "2026-03-11T01:00:01+01:00",
            }
            file_locks[filepath] = lock
            return True, lock, existing["session_id"] != session_id
        return False, existing, False

    def _release_file_lock(session_id: str, filepath: str, force: bool = False):
        existing = file_locks.get(filepath)
        if existing is None:
            return False
        if force or existing["session_id"] == session_id:
            del file_locks[filepath]
            return True
        return False

    def _list_file_locks():
        return [dict(v) for v in sorted(file_locks.values(), key=lambda item: item["filepath"])]

    def _list_file_locks_for_session(session_id: str):
        return [
            dict(v)
            for v in sorted(file_locks.values(), key=lambda item: item["filepath"])
            if v["session_id"] == session_id
        ]

    def _file_lock_count(session_id: str):
        return sum(1 for v in file_locks.values() if v["session_id"] == session_id)

    def _cleanup_stale_file_locks():
        removed = [dict(v) for v in file_locks.values() if v["session_id"] not in bridge.registry.sessions]
        for lock in removed:
            file_locks.pop(lock["filepath"], None)
        return sorted(removed, key=lambda item: item["filepath"])

    def _replace_todos(session_id: str, todos: list[dict], expected_version: int | None = None):
        current_version = int(bridge.registry.sessions[session_id].get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return None
        todos_by_session[session_id] = [
            {
                "content": str(todo.get("content", "")).strip(),
                "status": str(todo.get("status", "pending")),
                "priority": str(todo.get("priority", "medium")),
                "updated_at": "2026-03-11T01:00:00+01:00",
            }
            for todo in todos
        ]
        bridge.registry.sessions[session_id]["todos_version"] = current_version + 1
        return current_version + 1

    def _list_todos(session_id: str):
        return [dict(todo) for todo in todos_by_session.get(session_id, [])]

    def _todo_count(session_id: str):
        return len(todos_by_session.get(session_id, []))

    def _clear_todos(session_id: str):
        return len(todos_by_session.pop(session_id, []))

    def _add_todo(
        session_id: str,
        content: str,
        status: str = "pending",
        priority: str = "medium",
        expected_version: int | None = None,
    ):
        current_version = int(bridge.registry.sessions[session_id].get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return None, None
        todo = {
            "todo_id": uuid.uuid4().hex[:12],
            "content": content,
            "status": status,
            "priority": priority,
            "updated_at": "2026-03-11T01:00:00+01:00",
        }
        todos_by_session.setdefault(session_id, []).append(todo)
        bridge.registry.sessions[session_id]["todos_version"] = current_version + 1
        return dict(todo), bridge.registry.sessions[session_id]["todos_version"]

    def _update_todo(
        session_id: str,
        todo_id: str,
        *,
        content: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        expected_version: int | None = None,
    ):
        current_version = int(bridge.registry.sessions[session_id].get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return None, None
        for todo in todos_by_session.get(session_id, []):
            if todo["todo_id"] == todo_id:
                if content is not None:
                    todo["content"] = content
                if status is not None:
                    todo["status"] = status
                if priority is not None:
                    todo["priority"] = priority
                todo["updated_at"] = "2026-03-11T01:00:01+01:00"
                bridge.registry.sessions[session_id]["todos_version"] = current_version + 1
                return dict(todo), bridge.registry.sessions[session_id]["todos_version"]
        return None, None

    def _remove_todo(session_id: str, todo_id: str, expected_version: int | None = None):
        current_version = int(bridge.registry.sessions[session_id].get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return False, None
        items = todos_by_session.get(session_id, [])
        for idx, todo in enumerate(items):
            if todo["todo_id"] == todo_id:
                del items[idx]
                bridge.registry.sessions[session_id]["todos_version"] = current_version + 1
                return True, bridge.registry.sessions[session_id]["todos_version"]
        return False, None

    def _inbox_count(_session_id: str):
        return 0

    def _set_last_agent_sender(session_id: str, sender_session_id: str | None):
        if session_id not in bridge.registry.sessions:
            return False
        bridge.registry.sessions[session_id]["last_agent_sender"] = sender_session_id
        last_agent_sender_by_session[session_id] = sender_session_id
        return True

    def _get_last_agent_sender(session_id: str):
        return last_agent_sender_by_session.get(session_id)

    def _session_counts(session_id: str):
        return {
            "inbox_count": bridge.registry.inbox_count(session_id),
            "todo_count": bridge.registry.todo_count(session_id),
            "lock_count": bridge.registry.file_lock_count(session_id),
        }

    def _session_entry(
        session_id: str,
        info: dict,
        *,
        index: int | None = None,
        include_registered_at: bool = False,
        normalize_empty: bool = False,
    ):
        entry = {
            "session_id": session_id,
            "project": info["project"],
            "backend": (info.get("backend") or "null") if normalize_empty else info.get("backend"),
            "source": (info.get("source") or "") if normalize_empty else info.get("source"),
            "window": info.get("window") or "",
            "sty": info.get("sty") or "",
            "plugin_version": (info.get("plugin_version") or "") if normalize_empty else info.get("plugin_version"),
            "agent_state": (info.get("agent_state") or "") if normalize_empty else info.get("agent_state"),
            "agent_mode": (info.get("agent_mode") or "") if normalize_empty else info.get("agent_mode"),
            "todos_version": info.get("todos_version", 0),
            **_session_counts(session_id),
        }
        if index is not None:
            entry["index"] = index
        if include_registered_at:
            entry["registered_at"] = info["registered_at"]
        return entry

    def _session_context_payload(session_id: str, info: dict, *, normalize_empty: bool):
        return {
            "ok": True,
            "session": _session_entry(session_id, info, normalize_empty=normalize_empty),
            "todos": _list_todos(session_id),
            "file_locks": _list_file_locks_for_session(session_id),
        }

    def _legacy_project_matches(lock_project: str, lock_filepath: str, project: str):
        if not project:
            return True
        short = project.split("/")[-1]
        return lock_project == project or lock_project.endswith(short) or lock_filepath.startswith(project)

    def _read_legacy_lock_hints(project: str = ""):
        working = Path(os.path.expanduser("~/.claude/working"))
        if not working.is_dir():
            return []
        active_sessions = set(bridge.registry.sessions)
        locks = []
        for path in sorted(working.iterdir()):
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
            session_id = str(data.get("session_id", "")).strip()
            filepath = str(data.get("filepath", "")).strip()
            lock_project = str(data.get("project", "")).strip()
            if not session_id or not filepath:
                continue
            if not _legacy_project_matches(lock_project, filepath, project):
                continue
            locks.append(
                {
                    "session_id": session_id,
                    "filepath": filepath,
                    "project": lock_project,
                    "locked_at": str(data.get("locked_at", "")).strip(),
                    "stale": session_id not in active_sessions,
                    "source": "legacy",
                    "lockfile": str(path),
                }
            )
        return locks

    def _list_file_lock_payloads(*, project: str = "", include_stale: bool = True):
        locks = [
            {**dict(lock), "stale": lock["session_id"] not in bridge.registry.sessions, "source": "bridge"}
            for lock in _list_file_locks()
            if _legacy_project_matches(lock["project"], lock["filepath"], project)
        ]
        locks.extend(_read_legacy_lock_hints(project=project))
        if not include_stale:
            locks = [lock for lock in locks if not lock["stale"]]
        for lock in locks:
            lock.pop("lockfile", None)
        locks.sort(key=lambda item: (str(item.get("locked_at", "")), str(item.get("filepath", ""))))
        return locks

    def _cleanup_stale_lock_payloads(*, project: str = ""):
        removed = []
        for lock in _list_file_locks():
            if lock["session_id"] in bridge.registry.sessions:
                continue
            if not _legacy_project_matches(lock["project"], lock["filepath"], project):
                continue
            _release_file_lock(lock["session_id"], lock["filepath"], force=True)
            removed.append({**dict(lock), "stale": True, "source": "bridge"})
        for legacy_lock in _read_legacy_lock_hints(project=project):
            if not legacy_lock["stale"]:
                continue
            Path(str(legacy_lock["lockfile"])).unlink(missing_ok=True)
            result = dict(legacy_lock)
            result.pop("lockfile", None)
            removed.append(result)
        return removed

    bridge.registry.acquire_file_lock = MagicMock(side_effect=_acquire_file_lock)
    bridge.registry.release_file_lock = MagicMock(side_effect=_release_file_lock)
    bridge.registry.list_file_locks = MagicMock(side_effect=_list_file_locks)
    bridge.registry.list_file_locks_for_session = MagicMock(side_effect=_list_file_locks_for_session)
    bridge.registry.file_lock_count = MagicMock(side_effect=_file_lock_count)
    bridge.registry.cleanup_stale_file_locks = MagicMock(side_effect=_cleanup_stale_file_locks)
    bridge.registry.replace_todos = MagicMock(side_effect=_replace_todos)
    bridge.registry.list_todos = MagicMock(side_effect=_list_todos)
    bridge.registry.todo_count = MagicMock(side_effect=_todo_count)
    bridge.registry.clear_todos = MagicMock(side_effect=_clear_todos)
    bridge.registry.add_todo = MagicMock(side_effect=_add_todo)
    bridge.registry.update_todo = MagicMock(side_effect=_update_todo)
    bridge.registry.remove_todo = MagicMock(side_effect=_remove_todo)
    bridge.registry.inbox_count = MagicMock(side_effect=_inbox_count)
    bridge.registry.set_last_agent_sender = MagicMock(side_effect=_set_last_agent_sender)
    bridge.registry.get_last_agent_sender = MagicMock(side_effect=_get_last_agent_sender)
    bridge._stuff_to_session = AsyncMock(return_value=True)
    bridge._xmpp_send = MagicMock(return_value=True)
    bridge._session_prefix = MagicMock(side_effect=lambda info: f"[{info['project'].split('/')[-1]}]")
    bridge._session_counts = MagicMock(side_effect=_session_counts)
    bridge._session_entry = MagicMock(side_effect=_session_entry)
    bridge._session_context_payload = MagicMock(side_effect=_session_context_payload)
    bridge._list_file_lock_payloads = MagicMock(side_effect=_list_file_lock_payloads)
    bridge._cleanup_stale_lock_payloads = MagicMock(side_effect=_cleanup_stale_lock_payloads)
    bridge.audit = MagicMock()
    bridge.messages = MagicMock()
    bridge.messages.mcp_send_missing_to = "send_message requires 'to' (session_id)"
    bridge.messages.mcp_send_missing_message = "send_message requires 'message'"
    bridge.messages.mcp_send_target_not_found = "Target session not found: {to}"
    bridge.messages.mcp_send_no_backend = "Target session [{project}] has no multiplexer"
    bridge.messages.mcp_send_queued = "Message queued for {target_prefix}"
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

    def test_task_initially_none(self, server: BridgeMCPServer):
        assert server._task is None

    def test_mcp_initially_none(self, server: BridgeMCPServer):
        assert server._mcp is None


# ---------------------------------------------------------------------------
# enqueue / receive
# ---------------------------------------------------------------------------


class TestEnqueueAndReceive:
    def test_enqueue_calls_registry_inbox_put(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_put = MagicMock()
        started_server.enqueue("ses_AAA", "hello")
        started_server._bridge.registry.inbox_put.assert_called_once_with("ses_AAA", "hello", from_session=None)

    def test_enqueue_passes_from_session(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_put = MagicMock()
        started_server.enqueue("ses_AAA", "hello", from_session="ses_BBB")
        started_server._bridge.registry.inbox_put.assert_called_once_with("ses_AAA", "hello", from_session="ses_BBB")

    def test_enqueue_before_bridge_logs_warning_no_crash(self, server: BridgeMCPServer):
        """enqueue() before bridge is set should not raise."""
        server.enqueue("ses_AAA", "hello")  # must not raise

    def test_receive_calls_registry_inbox_drain(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(
            return_value=[("msg1", None), ("msg2", None)]
        )
        msgs = started_server._tool_receive_messages(session_id="ses_AAA")
        assert msgs == ["msg1", "msg2"]
        started_server._bridge.registry.inbox_drain_with_senders.assert_called_once_with("ses_AAA")

    def test_receive_drains_queue(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(
            return_value=[("msg1", None), ("msg2", None)]
        )
        msgs = started_server._tool_receive_messages(session_id="ses_AAA")
        assert msgs == ["msg1", "msg2"]

    def test_receive_empty_inbox_returns_empty_list(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(return_value=[])
        assert started_server._tool_receive_messages(session_id="ses_AAA") == []

    def test_receive_unknown_session_returns_empty(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(return_value=[])
        assert started_server._tool_receive_messages(session_id="ses_UNKNOWN") == []

    def test_receive_without_bridge_returns_empty(self, server: BridgeMCPServer):
        assert server._tool_receive_messages(session_id="ses_X") == []

    def test_receive_logs_audit_when_messages_present(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(return_value=[("hello", None)])
        started_server._tool_receive_messages(session_id="ses_AAA")
        started_server._bridge.audit.log.assert_called()
        event_arg = started_server._bridge.audit.log.call_args[0][0]
        assert event_arg == "MCP_RECEIVE"

    def test_receive_no_audit_when_empty(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(return_value=[])
        started_server._tool_receive_messages(session_id="ses_AAA")
        started_server._bridge.audit.log.assert_not_called()

    def test_receive_updates_last_agent_sender_from_inbox_metadata(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_drain_with_senders = MagicMock(return_value=[("hello", "ses_BBB")])
        started_server._bridge.registry.set_last_agent_sender = MagicMock(return_value=True)

        msgs = started_server._tool_receive_messages(session_id="ses_AAA")

        assert msgs == ["hello"]
        started_server._bridge.registry.set_last_agent_sender.assert_called_once_with("ses_AAA", "ses_BBB")


# ---------------------------------------------------------------------------
# send_message tool
# ---------------------------------------------------------------------------


class TestSendMessageTool:
    async def test_send_success(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="ping")
        assert "delivered" in result.lower() or "alpha" in result.lower()
        started_server._bridge._stuff_to_session.assert_awaited_once()

    async def test_send_screen_true_does_not_enqueue(self, started_server: BridgeMCPServer):
        """screen=True delivers via terminal only — MCP inbox must stay empty.

        Enqueueing screen-delivered messages would cause the idle-handler to
        re-inject them into the terminal on the next session.idle event,
        creating an infinite feedback loop (Bug #2 fix).
        """
        started_server._bridge.registry.inbox_put = MagicMock()
        await started_server._tool_send_message(to="ses_AAA", message="ping")
        started_server._bridge.registry.inbox_put.assert_not_called()

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
        payload = json.loads(call_arg)
        assert payload["type"] == "relay"
        assert payload["mode"] == "screen"
        assert payload["to"] == "ses_AAA"
        assert payload["message"] == "ping"
        assert "message_id" in payload

    async def test_send_includes_sender_session_id_in_relay_metadata(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(
            to="ses_AAA", message="ping", sender_session_id="ses_BBB", screen=False
        )
        args = started_server._bridge.registry.inbox_put.call_args[0]
        wrapped = args[1]
        lines = wrapped.splitlines()
        payload = json.loads(lines[1])
        assert payload["from"] == "ses_BBB"

    async def test_send_includes_sender_session_id_in_xmpp_notification(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(to="ses_AAA", message="ping", sender_session_id="ses_BBB")
        payload = json.loads(started_server._bridge._xmpp_send.call_args[0][0])
        assert payload["from"] == "ses_BBB"

    async def test_send_returns_message_id(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="ping")
        assert "[id:" in result

    async def test_send_screen_false_skips_relay(self, started_server: BridgeMCPServer):
        result = await started_server._tool_send_message(to="ses_AAA", message="ping", screen=False)
        started_server._bridge._stuff_to_session.assert_not_awaited()
        assert "inbox only" in result

    async def test_send_screen_false_enqueues(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_put = MagicMock()
        await started_server._tool_send_message(to="ses_AAA", message="ping", screen=False)
        args = started_server._bridge.registry.inbox_put.call_args[0]
        assert args[0] == "ses_AAA"
        assert "[bridge-generated message]" in args[1]
        assert args[1].endswith("ping")

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

    async def test_broadcast_does_not_enqueue_on_success(self, started_server: BridgeMCPServer):
        """Successful screen relay must NOT enqueue in MCP inbox (would cause double-delivery via pollInbox)."""
        started_server._bridge.registry.inbox_put = MagicMock()
        await started_server._tool_broadcast_message(message="broadcast msg", sender_session_id="ses_AAA")
        # ses_BBB received the message via screen relay — inbox_put must not have been called
        started_server._bridge.registry.inbox_put.assert_not_called()

    async def test_broadcast_enqueues_on_relay_failure(self, started_server: BridgeMCPServer):
        """Failed screen relay must enqueue in MCP inbox as fallback for pollInbox()."""
        started_server._bridge._stuff_to_session = AsyncMock(return_value=False)
        started_server._bridge.registry.inbox_put = MagicMock()
        await started_server._tool_broadcast_message(message="broadcast msg", sender_session_id="ses_AAA")
        # relay failed → inbox_put should have been called for ses_BBB
        args = started_server._bridge.registry.inbox_put.call_args[0]
        assert args[0] == "ses_BBB"
        assert "[bridge-generated message]" in args[1]
        assert args[1].endswith("broadcast msg")

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
        payload = json.loads(call_arg)
        assert payload["type"] == "broadcast"
        assert payload["mode"] == "screen"
        assert set(payload["to"]) == {"ses_AAA", "ses_BBB"}
        assert payload["message"] == "hi"

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

    def test_list_session_fields_includes_plugin_version(self, server: BridgeMCPServer):
        sessions = {"ses_X": _make_session_info(plugin_version="0.7.4")}
        server._bridge = _make_bridge(sessions=sessions)
        result = server._tool_list_sessions()
        assert "plugin_version" in result[0]
        assert result[0]["plugin_version"] == "0.7.4"

    def test_list_session_fields_includes_agent_state(self, server: BridgeMCPServer):
        sessions = {"ses_X": _make_session_info(agent_state="idle")}
        server._bridge = _make_bridge(sessions=sessions)
        result = server._tool_list_sessions()
        assert "agent_state" in result[0]
        assert result[0]["agent_state"] == "idle"

    def test_list_plugin_version_none_returns_empty_string(self, server: BridgeMCPServer):
        sessions = {"ses_X": _make_session_info(plugin_version=None)}
        server._bridge = _make_bridge(sessions=sessions)
        result = server._tool_list_sessions()
        assert result[0]["plugin_version"] == ""

    def test_list_includes_inbox_todo_and_lock_counts(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.inbox_count = MagicMock(return_value=2)
        started_server._bridge.registry.todo_count = MagicMock(return_value=3)
        started_server._bridge.registry.file_lock_count = MagicMock(return_value=4)
        result = started_server._tool_list_sessions()
        assert result[0]["inbox_count"] == 2
        assert result[0]["todo_count"] == 3
        assert result[0]["lock_count"] == 4


class TestTodoContextTools:
    def test_replace_todos_succeeds(self, started_server: BridgeMCPServer):
        result = started_server._tool_replace_todos(
            session_id="ses_AAA",
            todos=[{"content": "first", "status": "pending", "priority": "high"}],
        )
        assert result == {"ok": True, "count": 1, "version": 1}

    def test_replace_todos_detects_version_conflict(self, started_server: BridgeMCPServer):
        started_server._tool_replace_todos(
            session_id="ses_AAA",
            todos=[{"content": "first", "status": "pending", "priority": "high"}],
        )
        result = started_server._tool_replace_todos(
            session_id="ses_AAA",
            todos=[{"content": "second", "status": "pending", "priority": "high"}],
            expected_version=0,
        )
        assert result["ok"] is False
        assert result["error"] == "todo version conflict"
        assert result["current_version"] == 1

    def test_list_todos_returns_saved_items(self, started_server: BridgeMCPServer):
        started_server._tool_replace_todos(
            session_id="ses_AAA",
            todos=[{"content": "first", "status": "pending", "priority": "high"}],
        )
        todos = started_server._tool_list_todos(session_id="ses_AAA")
        assert len(todos) == 1
        assert todos[0]["content"] == "first"

    def test_get_session_context_returns_counts_todos_and_locks(self, started_server: BridgeMCPServer):
        started_server._tool_replace_todos(
            session_id="ses_AAA",
            todos=[{"content": "first", "status": "pending", "priority": "high"}],
        )
        started_server._tool_acquire_file_lock(session_id="ses_AAA", filepath="/tmp/a.py")
        started_server._bridge.registry.inbox_count = MagicMock(return_value=2)

        result = started_server._tool_get_session_context(session_id="ses_AAA")

        assert result["ok"] is True
        assert result["session"]["session_id"] == "ses_AAA"
        assert result["session"]["inbox_count"] == 2
        assert result["session"]["todo_count"] == 1
        assert result["session"]["lock_count"] == 1
        assert result["session"]["todos_version"] == 1
        assert result["todos"][0]["content"] == "first"
        assert result["file_locks"][0]["filepath"] == "/tmp/a.py"

    def test_get_session_context_unknown_session(self, started_server: BridgeMCPServer):
        result = started_server._tool_get_session_context(session_id="ses_UNKNOWN")
        assert result["ok"] is False
        assert "unknown session_id" in result["error"]

    def test_add_update_remove_todo(self, started_server: BridgeMCPServer):
        added = started_server._tool_add_todo(session_id="ses_AAA", content="first", priority="high")
        assert added["ok"] is True
        todo_id = added["todo"]["todo_id"]

        updated = started_server._tool_update_todo(session_id="ses_AAA", todo_id=todo_id, status="completed")
        assert updated["ok"] is True
        assert updated["todo"]["status"] == "completed"

        removed = started_server._tool_remove_todo(session_id="ses_AAA", todo_id=todo_id)
        assert removed["ok"] is True
        assert removed["removed"] is True

    def test_atomic_todo_ops_respect_expected_version(self, started_server: BridgeMCPServer):
        added = started_server._tool_add_todo(session_id="ses_AAA", content="first")
        todo_id = added["todo"]["todo_id"]

        conflict = started_server._tool_update_todo(
            session_id="ses_AAA",
            todo_id=todo_id,
            status="completed",
            expected_version=0,
        )
        assert conflict["ok"] is False
        assert conflict["error"] == "todo version conflict"

        conflict_remove = started_server._tool_remove_todo(
            session_id="ses_AAA",
            todo_id=todo_id,
            expected_version=0,
        )
        assert conflict_remove["ok"] is False
        assert conflict_remove["error"] == "todo version conflict"

        conflict_add = started_server._tool_add_todo(
            session_id="ses_AAA",
            content="second",
            expected_version=0,
        )
        assert conflict_add["ok"] is False
        assert conflict_add["error"] == "todo version conflict"


# ---------------------------------------------------------------------------
# file lock tools
# ---------------------------------------------------------------------------


class TestFileLockTools:
    def test_acquire_file_lock_succeeds(self, started_server: BridgeMCPServer):
        result = started_server._tool_acquire_file_lock(
            session_id="ses_AAA", filepath="/tmp/a.py", project="", reason="edit"
        )
        assert result["ok"] is True
        assert result["lock"]["session_id"] == "ses_AAA"
        assert result["lock"]["filepath"] == "/tmp/a.py"
        assert result["lock"]["reason"] == "edit"

    def test_acquire_file_lock_reports_conflict(self, started_server: BridgeMCPServer):
        started_server._tool_acquire_file_lock(session_id="ses_AAA", filepath="/tmp/a.py")
        result = started_server._tool_acquire_file_lock(session_id="ses_BBB", filepath="/tmp/a.py")
        assert result["ok"] is False
        assert result["lock"]["session_id"] == "ses_AAA"

    def test_release_file_lock_succeeds(self, started_server: BridgeMCPServer):
        started_server._tool_acquire_file_lock(session_id="ses_AAA", filepath="/tmp/a.py")
        result = started_server._tool_release_file_lock(session_id="ses_AAA", filepath="/tmp/a.py")
        assert result == {"ok": True, "released": True}

    def test_list_file_locks_returns_active_and_stale(self, started_server: BridgeMCPServer, tmp_path):
        started_server._tool_acquire_file_lock(session_id="ses_AAA", filepath="/tmp/native.py")
        working = tmp_path / ".claude" / "working"
        working.mkdir(parents=True)
        (working / "stale---b.py").write_text(
            json.dumps(
                {
                    "session_id": "ses_STALE",
                    "filepath": "/tmp/b.py",
                    "project": "~/alpha",
                    "locked_at": "2026-03-11T01:01:00+01:00",
                }
            )
        )

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            locks = started_server._tool_list_file_locks(project="", include_stale=True)

        assert len(locks) == 2
        assert {(lock["session_id"], lock["source"]) for lock in locks} == {
            ("ses_AAA", "bridge"),
            ("ses_STALE", "legacy"),
        }
        stale = {(lock["session_id"], lock["source"]): lock["stale"] for lock in locks}
        assert stale[("ses_AAA", "bridge")] is False
        assert stale[("ses_STALE", "legacy")] is True

    def test_list_file_locks_can_hide_stale(self, started_server: BridgeMCPServer, tmp_path):
        working = tmp_path / ".claude" / "working"
        working.mkdir(parents=True)
        (working / "stale---b.py").write_text(
            json.dumps(
                {
                    "session_id": "ses_STALE",
                    "filepath": "/tmp/b.py",
                    "project": "~/alpha",
                    "locked_at": "2026-03-11T01:01:00+01:00",
                }
            )
        )

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            locks = started_server._tool_list_file_locks(include_stale=False)

        assert locks == []

    def test_cleanup_stale_locks_removes_only_stale(self, started_server: BridgeMCPServer, tmp_path):
        started_server._tool_acquire_file_lock(session_id="ses_AAA", filepath="/tmp/native.py")
        started_server._bridge.registry.register("ses_STALE", "1", "1", "/tmp/stale")
        started_server._bridge.registry.acquire_file_lock("ses_STALE", "/tmp/native-stale.py", "/tmp/stale")
        started_server._bridge.registry.unregister("ses_STALE")
        working = tmp_path / ".claude" / "working"
        working.mkdir(parents=True)
        stale = working / "stale---b.py"
        stale.write_text(
            json.dumps(
                {
                    "session_id": "ses_STALE",
                    "filepath": "/tmp/b.py",
                    "project": "~/alpha",
                    "locked_at": "2026-03-11T01:01:00+01:00",
                }
            )
        )

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = started_server._tool_cleanup_stale_locks(project="")

        assert result["removed"] == 2
        assert {lock["filepath"] for lock in result["locks"]} == {"/tmp/b.py", "/tmp/native-stale.py"}
        assert started_server._bridge.registry.list_file_locks()[0]["filepath"] == "/tmp/native.py"
        assert not stale.exists()
        started_server._bridge.audit.log.assert_called()

    def test_cleanup_stale_locks_respects_project_filter(self, started_server: BridgeMCPServer, tmp_path):
        started_server._bridge.registry.register("ses_STALE_A", "1", "1", "/tmp/proj-a")
        started_server._bridge.registry.register("ses_STALE_B", "1", "1", "/tmp/proj-b")
        started_server._bridge.registry.acquire_file_lock("ses_STALE_A", "/tmp/a.py", "/tmp/proj-a")
        started_server._bridge.registry.acquire_file_lock("ses_STALE_B", "/tmp/b.py", "/tmp/proj-b")
        started_server._bridge.registry.unregister("ses_STALE_A")
        started_server._bridge.registry.unregister("ses_STALE_B")

        working = tmp_path / ".claude" / "working"
        working.mkdir(parents=True)
        stale_a = working / "stale-a---a.py"
        stale_b = working / "stale-b---b.py"
        stale_a.write_text(
            json.dumps(
                {
                    "session_id": "ses_STALE_A",
                    "filepath": "/tmp/a-legacy.py",
                    "project": "/tmp/proj-a",
                    "locked_at": "2026-03-11T01:01:00+01:00",
                }
            )
        )
        stale_b.write_text(
            json.dumps(
                {
                    "session_id": "ses_STALE_B",
                    "filepath": "/tmp/b-legacy.py",
                    "project": "/tmp/proj-b",
                    "locked_at": "2026-03-11T01:01:01+01:00",
                }
            )
        )

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = started_server._tool_cleanup_stale_locks(project="/tmp/proj-a")

        assert {lock["filepath"] for lock in result["locks"]} == {"/tmp/a-legacy.py", "/tmp/a.py"}
        assert stale_b.exists()

    def test_list_agent_state_none_returns_empty_string(self, server: BridgeMCPServer):
        sessions = {"ses_X": _make_session_info(agent_state=None)}
        server._bridge = _make_bridge(sessions=sessions)
        result = server._tool_list_sessions()
        assert result[0]["agent_state"] == ""


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
        assert "reply_to_last_sender" in tool_names
        assert "broadcast_message" in tool_names
        assert "list_sessions" in tool_names
        assert "get_session_context" in tool_names
        assert "list_todos" in tool_names
        assert "replace_todos" in tool_names
        assert "add_todo" in tool_names
        assert "update_todo" in tool_names
        assert "remove_todo" in tool_names
        assert "list_file_locks" in tool_names
        assert "acquire_file_lock" in tool_names
        assert "release_file_lock" in tool_names
        assert "cleanup_stale_locks" in tool_names

    def test_build_mcp_uses_correct_port(self, started_server: BridgeMCPServer):
        mcp = started_server._build_mcp()
        assert mcp.settings.port == started_server.port


class TestReplyToLastSenderTool:
    async def test_reply_to_last_sender_uses_stored_sender(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.set_last_agent_sender("ses_AAA", "ses_BBB")
        started_server._bridge._nudge_session = AsyncMock(return_value=True)

        result = await started_server._tool_reply_to_last_sender(
            session_id="ses_AAA", message="reply back", nudge=True
        )

        assert "nudge" in result.lower()
        wrapped = started_server._bridge._nudge_session.await_args.args[2]
        payload = json.loads(wrapped.splitlines()[1])
        assert payload["from"] == "ses_AAA"
        assert payload["to"] == "ses_BBB"

    async def test_reply_to_last_sender_without_known_sender_returns_error(self, started_server: BridgeMCPServer):
        result = await started_server._tool_reply_to_last_sender(
            session_id="ses_AAA", message="reply back", nudge=True
        )
        assert "no known sender" in result.lower()


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
        bridge.mcp_server.enqueue.assert_called_once_with("ses_TEST", "hello", from_session=None)

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


# ---------------------------------------------------------------------------
# send_message nudge mode
# ---------------------------------------------------------------------------


class TestSendMessageNudge:
    async def test_send_nudge_true_calls_nudge_session(self, started_server: BridgeMCPServer):
        """nudge=True must call _nudge_session, not _stuff_to_session."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        result = await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True)
        started_server._bridge._nudge_session.assert_awaited_once()
        started_server._bridge._stuff_to_session.assert_not_awaited()
        assert "nudge" in result.lower() or "delivered" in result.lower() or "alpha" in result.lower()

    async def test_send_screen_true_wraps_generated_message(self, started_server: BridgeMCPServer):
        await started_server._tool_send_message(to="ses_AAA", message="ping")
        wrapped = started_server._bridge._stuff_to_session.await_args.args[2]
        assert "[bridge-generated message]" in wrapped
        assert wrapped.endswith("ping")

    async def test_send_nudge_true_wraps_generated_message(self, started_server: BridgeMCPServer):
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True)
        wrapped = started_server._bridge._nudge_session.await_args.args[2]
        assert "[bridge-generated message]" in wrapped
        assert wrapped.endswith("ping")

    async def test_send_nudge_true_returns_message_id_with_nudge_tag(self, started_server: BridgeMCPServer):
        """nudge=True confirmation string must include both [id:...] and (nudge)."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        result = await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True)
        assert "[id:" in result
        assert "nudge" in result.lower()

    async def test_send_nudge_true_no_backend_queues_message(self, started_server: BridgeMCPServer):
        """nudge=True for a session without a backend should still queue the
        message instead of failing outright.
        """
        started_server._bridge.registry.sessions["ses_NOBACK"] = _make_session_info(
            project="/home/user/noback", backend=None
        )
        started_server._bridge.registry.get = MagicMock(
            side_effect=lambda sid: started_server._bridge.registry.sessions.get(sid)
        )
        result = await started_server._tool_send_message(to="ses_NOBACK", message="ping", nudge=True)
        assert "queued" in result.lower() or "nudge" in result.lower()
        started_server._bridge._nudge_session.assert_not_called()

    async def test_send_nudge_true_backend_does_not_double_enqueue(self, started_server: BridgeMCPServer):
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        started_server._bridge.registry.inbox_put = MagicMock()

        await started_server._tool_send_message(
            to="ses_AAA", message="ping", nudge=True, sender_session_id="ses_BBB"
        )

        started_server._bridge.registry.inbox_put.assert_not_called()
        assert started_server._bridge._nudge_session.await_args.kwargs["from_session"] == "ses_BBB"

    async def test_send_nudge_true_failure_returns_error(self, started_server: BridgeMCPServer):
        """nudge=True when CR send fails must return a failure message."""
        started_server._bridge._nudge_session = AsyncMock(return_value=False)
        result = await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True)
        assert "failed" in result.lower() or "delivery" in result.lower()

    async def test_send_nudge_true_notifies_xmpp(self, started_server: BridgeMCPServer):
        """nudge=True must send an XMPP notification to the observer."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True)
        started_server._bridge._xmpp_send.assert_called_once()
        call_arg = started_server._bridge._xmpp_send.call_args[0][0]
        payload = json.loads(call_arg)
        assert payload["type"] == "relay"
        assert payload["mode"] == "nudge"
        assert payload["to"] == "ses_AAA"
        assert payload["message"] == "ping"
        assert "message_id" in payload

    async def test_send_nudge_takes_priority_over_screen(self, started_server: BridgeMCPServer):
        """nudge=True must use nudge even when screen=True is also set."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True, screen=True)
        started_server._bridge._nudge_session.assert_awaited_once()
        started_server._bridge._stuff_to_session.assert_not_awaited()

    async def test_send_nudge_logs_audit(self, started_server: BridgeMCPServer):
        """nudge=True must emit a MCP_SEND audit event."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        await started_server._tool_send_message(to="ses_AAA", message="ping", nudge=True)
        started_server._bridge.audit.log.assert_called()
        event_arg = started_server._bridge.audit.log.call_args[0][0]
        assert event_arg == "MCP_SEND"


# ---------------------------------------------------------------------------
# broadcast_message nudge mode
# ---------------------------------------------------------------------------


class TestBroadcastMessageNudge:
    async def test_broadcast_nudge_true_calls_nudge_session(self, started_server: BridgeMCPServer):
        """nudge=True broadcast must call _nudge_session for each target."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        result = await started_server._tool_broadcast_message(
            message="nudge all", sender_session_id="ses_AAA", nudge=True
        )
        assert started_server._bridge._nudge_session.await_count == 1  # ses_BBB only (ses_AAA excluded)
        started_server._bridge._stuff_to_session.assert_not_awaited()
        assert "1" in result

    async def test_broadcast_nudge_true_does_not_call_enqueue_on_success(self, started_server: BridgeMCPServer):
        """nudge=True broadcast must NOT call self.enqueue — _nudge_session handles inbox internally."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        started_server._bridge.registry.inbox_put = MagicMock()
        await started_server._tool_broadcast_message(message="nudge", sender_session_id="ses_AAA", nudge=True)
        # inbox_put must NOT be called directly by broadcast tool when nudge=True
        started_server._bridge.registry.inbox_put.assert_not_called()

    async def test_broadcast_nudge_xmpp_contains_nudge_tag(self, started_server: BridgeMCPServer):
        """nudge=True broadcast XMPP notification must include '(nudge)' tag."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        await started_server._tool_broadcast_message(message="nudge all", sender_session_id="", nudge=True)
        started_server._bridge._xmpp_send.assert_called_once()
        call_arg = started_server._bridge._xmpp_send.call_args[0][0]
        assert "nudge" in call_arg.lower()

    async def test_broadcast_nudge_logs_audit(self, started_server: BridgeMCPServer):
        """nudge=True broadcast must emit a MCP_BROADCAST audit event."""
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        await started_server._tool_broadcast_message(message="nudge", sender_session_id="", nudge=True)
        started_server._bridge.audit.log.assert_called()
        event_arg = started_server._bridge.audit.log.call_args[0][0]
        assert event_arg == "MCP_BROADCAST"

    async def test_broadcast_nudge_true_enqueues_for_no_backend_targets(self, started_server: BridgeMCPServer):
        started_server._bridge.registry.sessions["ses_NOBACK"] = _make_session_info(
            project="/home/user/noback", backend=None, sty="", window=""
        )
        started_server._bridge.registry.get = MagicMock(
            side_effect=lambda sid: started_server._bridge.registry.sessions.get(sid)
        )
        started_server._bridge._nudge_session = AsyncMock(return_value=True)
        started_server._bridge.registry.inbox_put = MagicMock()

        result = await started_server._tool_broadcast_message(
            message="nudge all", sender_session_id="ses_AAA", nudge=True
        )

        assert "2" in result
        assert started_server._bridge._nudge_session.await_count == 1
        started_server._bridge.registry.inbox_put.assert_called_once()
        args = started_server._bridge.registry.inbox_put.call_args[0]
        assert args[0] == "ses_NOBACK"
        assert args[1].endswith("nudge all")


class TestTodoToolErrors:
    def test_update_todo_unknown_session(self, started_server: BridgeMCPServer):
        result = started_server._tool_update_todo(session_id="ses_UNKNOWN", todo_id="todo-1", status="done")
        assert result["ok"] is False
        assert result["error"] == "unknown session_id: ses_UNKNOWN"

    def test_remove_todo_unknown_session(self, started_server: BridgeMCPServer):
        result = started_server._tool_remove_todo(session_id="ses_UNKNOWN", todo_id="todo-1")
        assert result["ok"] is False
        assert result["error"] == "unknown session_id: ses_UNKNOWN"
