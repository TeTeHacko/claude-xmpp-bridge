"""MCP server — exposes bridge functionality as Model Context Protocol tools.

The server runs as an asyncio task inside the bridge process, sharing the same
SessionRegistry and message-delivery logic.  It listens on localhost:7878 by
default using the streamable-http transport (a single /mcp endpoint).

Tools:
  send_message(to, message, screen)  — deliver a message to a specific agent session
  broadcast_message(message)         — deliver a message to all registered sessions
  receive_messages(session_id)       — drain the inbox queue for a session
  list_sessions()                    — list all registered sessions

The ``send_message`` and ``broadcast_message`` tools use the same screen relay
mechanism as the existing socket relay/broadcast commands.  Received messages
are persisted in SQLite (bridge.db inbox table); ``receive_messages`` drains
and returns them atomically.  Messages survive bridge restarts.

OpenCode plugin integration:
  - On ``session.idle`` the plugin calls ``receive_messages`` and injects any
    pending messages via screen relay (existing mechanism).
  - Agents can also call ``receive_messages`` proactively at any time.

Configuration:
  Port is set via Config.mcp_port (default 7878).  Set to 0 to disable.

Audit log:
  All MCP tool invocations are recorded via AuditLogger with event types:
    MCP_SEND        — send_message called (success or failure)
    MCP_BROADCAST   — broadcast_message called
    MCP_RECEIVE     — receive_messages drained inbox
  Each send event generates a unique message_id (UUID4 short hex) returned
  in the confirmation string so senders can correlate ACK replies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from .messages import format_generated_agent_message

if TYPE_CHECKING:
    from .bridge import XMPPBridge

log = logging.getLogger(__name__)


class BridgeMCPServer:
    """MCP server that exposes bridge tools to OpenCode agents.

    Lifecycle:
      - Instantiated by XMPPBridge.__init__ (no I/O yet)
      - Started via ``start(bridge)`` as an asyncio task in bridge.run()
      - Stopped gracefully via ``stop()`` during bridge.shutdown()

    The ``bridge`` reference is passed to ``start()`` rather than ``__init__``
    to avoid a circular import at module level (bridge imports mcp_server, mcp_server
    would import bridge).

    Inbox persistence:
      Messages are stored in the SQLite ``inbox`` table (registry.db) rather than
      in-memory asyncio queues.  This means messages survive bridge restarts and
      are not lost if the bridge process is killed.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._bridge: XMPPBridge | None = None
        self._task: asyncio.Task[None] | None = None
        self._mcp: FastMCP | None = None

    # ------------------------------------------------------------------
    # Public API used by XMPPBridge
    # ------------------------------------------------------------------

    def enqueue(self, session_id: str, message: str, *, from_session: str | None = None) -> None:
        """Put a message into the SQLite inbox for *session_id*.

        Called from bridge relay/broadcast handlers so that MCP clients
        can also receive inter-agent messages via ``receive_messages``.
        If the inbox is full the oldest message is dropped to make room.
        """
        if self._bridge is None:
            log.warning("enqueue called before bridge initialised — dropping message for %s", session_id)
            return
        self._bridge.registry.inbox_put(session_id, message, from_session=from_session)

    async def start(self, bridge: XMPPBridge) -> None:
        """Initialise the FastMCP server and launch it as a background task."""
        self._bridge = bridge
        self._mcp = self._build_mcp()
        self._task = asyncio.create_task(self._serve(), name="mcp-server")
        log.info("MCP server task started on port %d", self.port)

    async def stop(self) -> None:
        """Cancel the background server task."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        log.info("MCP server stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_mcp(self) -> FastMCP:
        """Construct and register the FastMCP instance with all tools."""
        mcp = FastMCP(
            name="xmpp-bridge",
            host="127.0.0.1",
            port=self.port,
            log_level="WARNING",  # avoid noisy uvicorn INFO in bridge logs
        )

        # Keep a reference to self that tool functions can close over.
        server = self

        @mcp.tool()
        async def send_message(
            to: str,
            message: str,
            screen: bool = True,
            nudge: bool = False,
            sender_session_id: str = "",
        ) -> str:
            """Send a message to a specific agent session identified by session_id.

            Delivery modes (mutually exclusive; nudge takes priority over screen):
              - nudge=True  : store message in SQLite inbox, send bare CR to wake the
                              agent.  The agent's plugin picks up the message on the
                              next session.idle via receive_messages().  This avoids the
                              race condition where a screen inject interrupts an agent
                              mid-task.  Recommended for all inter-agent communication.
              - screen=True : deliver via the terminal multiplexer immediately (default
                              when nudge=False).  Fast but can interfere if the agent is
                              currently executing tool calls.
              - screen=False: only enqueue in MCP inbox, no terminal interaction at all.
                              Useful when the target agent polls frequently on its own.

            The bridge also sends an XMPP notification to the human observer.
            The returned confirmation string includes a unique ``message_id`` that the
            recipient can reference in an ACK reply (``ack:<message_id>``).

            Args:
                to: Target session_id (as shown by list_sessions).
                message: Text to deliver to the target agent.
                nudge: If True, store in inbox and send CR nudge only (recommended).
                screen: If True (default), deliver via screen/tmux relay (when nudge=False).
                        If False, only enqueue in MCP inbox (no screen relay, no nudge).
                sender_session_id: Optional sender session_id to include in relay metadata
                        so the recipient can reply directly to the originating agent.

            Returns:
                A confirmation string with message_id on success, or an error description.
            """
            return await server._tool_send_message(
                to=to,
                message=message,
                screen=screen,
                nudge=nudge,
                sender_session_id=sender_session_id,
            )

        @mcp.tool()
        async def broadcast_message(message: str, sender_session_id: str = "", nudge: bool = False) -> str:
            """Broadcast a message to all registered agent sessions.

            The message is delivered to every session that has a backend.  The sender
            session (if provided) is excluded from delivery so an agent does not echo
            its own broadcast to itself.

            Args:
                message: Text to deliver to all agents.
                sender_session_id: Optional — caller's own session_id to exclude from delivery.
                nudge: If True, store in each inbox and send CR nudge only (recommended).
                       If False (default), deliver via terminal multiplexer immediately.

            Returns:
                A summary string with delivery count.
            """
            return await server._tool_broadcast_message(
                message=message, sender_session_id=sender_session_id, nudge=nudge
            )

        @mcp.tool()
        async def receive_messages(session_id: str) -> list[str]:
            """Drain and return all pending messages in the inbox for *session_id*.

            Messages are queued here when another agent calls ``send_message`` or
            ``broadcast_message`` targeting this session.  After this call the inbox
            is empty (messages are consumed).

            Args:
                session_id: Your own session_id (as shown by list_sessions).

            Returns:
                List of pending message strings (may be empty).
            """
            return server._tool_receive_messages(session_id=session_id)

        @mcp.tool()
        async def reply_to_last_sender(session_id: str, message: str, nudge: bool = True) -> str:
            """Reply to the last agent session that messaged *session_id*.

            The bridge remembers the latest non-null relay sender seen by
            ``receive_messages(session_id)``. This helper resolves that sender and
            forwards the reply back to them, setting ``sender_session_id`` to the
            replying session so the conversation can continue agent-to-agent.
            """
            return await server._tool_reply_to_last_sender(
                session_id=session_id,
                message=message,
                nudge=nudge,
            )

        @mcp.tool()
        async def list_sessions() -> list[dict[str, Any]]:
            """List all currently registered agent sessions.

            Returns a list of session objects, each with:
              - session_id: unique identifier
              - project: working directory path
              - backend: multiplexer type (screen/tmux/null)
              - source: agent type (opencode/etc)
              - window: terminal window number (for screen)
              - plugin_version: version of the OpenCode plugin (if reported)
              - agent_state: last known agent state ("idle", "running", etc.)

            Returns:
                List of session dicts.
            """
            return server._tool_list_sessions()

        @mcp.tool()
        async def get_session_context(session_id: str) -> dict[str, Any]:
            """Return the current coordination context for one session.

            Includes session metadata, inbox/todo/lock counts, current todo list,
            and bridge-native file locks held by the session.
            """
            return server._tool_get_session_context(session_id=session_id)

        @mcp.tool()
        async def list_todos(session_id: str) -> list[dict[str, Any]]:
            """Return the stored todo list for one session."""
            return server._tool_list_todos(session_id=session_id)

        @mcp.tool()
        async def replace_todos(
            session_id: str, todos: list[dict[str, Any]], expected_version: int | None = None
        ) -> dict[str, Any]:
            """Replace the stored todo list for one session.

            If ``expected_version`` is provided, the update only succeeds when it
            matches the current todo version for that session.
            """
            return server._tool_replace_todos(
                session_id=session_id, todos=todos, expected_version=expected_version
            )

        @mcp.tool()
        async def add_todo(
            session_id: str,
            content: str,
            status: str = "pending",
            priority: str = "medium",
            expected_version: int | None = None,
        ) -> dict[str, Any]:
            """Append one todo item to a session todo list."""
            return server._tool_add_todo(
                session_id=session_id,
                content=content,
                status=status,
                priority=priority,
                expected_version=expected_version,
            )

        @mcp.tool()
        async def update_todo(
            session_id: str,
            todo_id: str,
            content: str | None = None,
            status: str | None = None,
            priority: str | None = None,
            expected_version: int | None = None,
        ) -> dict[str, Any]:
            """Update one todo item by id."""
            return server._tool_update_todo(
                session_id=session_id,
                todo_id=todo_id,
                content=content,
                status=status,
                priority=priority,
                expected_version=expected_version,
            )

        @mcp.tool()
        async def remove_todo(session_id: str, todo_id: str, expected_version: int | None = None) -> dict[str, Any]:
            """Remove one todo item by id."""
            return server._tool_remove_todo(
                session_id=session_id,
                todo_id=todo_id,
                expected_version=expected_version,
            )

        @mcp.tool()
        async def list_file_locks(project: str = "", include_stale: bool = True) -> list[dict[str, Any]]:
            """List file lock hints from ``~/.claude/working``.

            Args:
                project: Optional project filter. Matches either the stored short
                    project path (``~/foo``) or an absolute filepath prefix.
                include_stale: If False, omit locks whose ``session_id`` is not
                    currently registered in the bridge.

            Returns:
                List of lock dicts with ``session_id``, ``filepath``, ``project``,
                ``locked_at`` and ``stale``.
            """
            return server._tool_list_file_locks(project=project, include_stale=include_stale)

        @mcp.tool()
        async def acquire_file_lock(
            session_id: str, filepath: str, project: str = "", reason: str = ""
        ) -> dict[str, Any]:
            """Acquire a bridge-native file lock for a session.

            If the file is already locked by another active session, returns the
            current owner instead of replacing it.
            """
            return server._tool_acquire_file_lock(
                session_id=session_id, filepath=filepath, project=project, reason=reason
            )

        @mcp.tool()
        async def release_file_lock(session_id: str, filepath: str, force: bool = False) -> dict[str, Any]:
            """Release a bridge-native file lock for a session."""
            return server._tool_release_file_lock(session_id=session_id, filepath=filepath, force=force)

        @mcp.tool()
        async def cleanup_stale_locks(project: str = "") -> dict[str, Any]:
            """Remove stale file lock hints from ``~/.claude/working``.

            A lock is stale when its ``session_id`` is no longer present in the
            bridge session registry.

            Args:
                project: Optional project filter. If set, only stale locks for
                    that project/path are removed.

            Returns:
                Summary dict with removed count and removed lock entries.
            """
            return server._tool_cleanup_stale_locks(project=project)

        return mcp

    async def _serve(self) -> None:
        """Run the FastMCP streamable-http server (blocks until cancelled)."""
        if self._mcp is None:  # pragma: no cover
            return
        try:
            await self._mcp.run_streamable_http_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MCP server crashed")
            raise

    # ------------------------------------------------------------------
    # Tool implementations (separated from tool registration for testability)
    # ------------------------------------------------------------------

    def _short_path(self, path: str) -> str:
        """Replace $HOME with ~ for display. Delegates to bridge if available."""
        if self._bridge is not None:
            return self._bridge._short_path(path)
        home = os.path.expanduser("~")
        if path == home:
            return "~"
        return path.replace(home + "/", "~/", 1) if path.startswith(home + "/") else path

    @staticmethod
    def _lock_dir() -> Path:
        """Return the directory that stores file-lock hint files."""
        return Path.home() / ".claude" / "working"

    def _project_matches(self, lock_project: str, lock_filepath: str, project: str) -> bool:
        """Return True if *lock* belongs to the requested project filter."""
        if not project:
            return True
        short = self._short_path(project)
        return lock_project in {project, short} or lock_filepath.startswith(project)

    def _read_file_locks(self, *, project: str = "") -> list[dict[str, Any]]:
        """Read legacy file-lock hint files from ``~/.claude/working``."""
        bridge = self._bridge
        active_sessions = set(bridge.registry.sessions) if bridge is not None else set()
        lock_dir = self._lock_dir()
        if not lock_dir.is_dir():
            return []

        locks: list[dict[str, Any]] = []
        for path in sorted(lock_dir.iterdir()):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            session_id = str(data.get("session_id", "")).strip()
            filepath = str(data.get("filepath", "")).strip()
            lock_project = str(data.get("project", "")).strip()
            locked_at = str(data.get("locked_at", "")).strip()
            if not session_id or not filepath:
                continue
            if not self._project_matches(lock_project, filepath, project):
                continue
            locks.append(
                {
                    "session_id": session_id,
                    "filepath": filepath,
                    "project": lock_project,
                    "locked_at": locked_at,
                    "stale": session_id not in active_sessions,
                    "source": "legacy",
                    "lockfile": str(path),
                }
            )
        locks.sort(key=lambda item: (item["locked_at"], item["filepath"]))
        return locks

    async def _tool_send_message(
        self,
        *,
        to: str,
        message: str,
        screen: bool = True,
        nudge: bool = False,
        sender_session_id: str = "",
    ) -> str:
        """Implementation of the send_message tool."""
        bridge = self._bridge
        if bridge is None:
            return "Error: bridge not initialised"
        if not to:
            return bridge.messages.mcp_send_missing_to
        if not message:
            return bridge.messages.mcp_send_missing_message

        target_info = bridge.registry.get(to)
        if not target_info:
            return bridge.messages.mcp_send_target_not_found.format(to=to)

        target_prefix = bridge._session_prefix(target_info)
        message_id = uuid.uuid4().hex[:12]
        wrapped_message = format_generated_agent_message(
            msg_type="relay",
            message=message,
            from_session_id=sender_session_id or None,
            to_session_id=to,
            mode="nudge" if nudge else ("screen" if screen else "inbox"),
            message_id=message_id,
        )

        if nudge:
            # nudge=True: prefer inbox + CR nudge for interactive sessions,
            # but still queue for backend-less sessions so MCP-only agents can
            # pick the message up on their next poll.
            if target_info["backend"]:
                ok = await bridge._nudge_session(
                    to,
                    target_info,
                    wrapped_message,
                    from_session=sender_session_id or None,
                )
            else:
                self.enqueue(to, wrapped_message, from_session=sender_session_id or None)
                ok = True
            bridge._xmpp_send(
                json.dumps(
                        {
                            "type": "relay",
                            "mode": "nudge",
                            "from": sender_session_id or None,
                            "to": to,
                            "message_id": message_id,
                            "message": message,
                        "ts": time.time(),
                    },
                    ensure_ascii=False,
                )
            )
            bridge.audit.log(
                "MCP_SEND",
                    message_id=message_id,
                    from_session_id=sender_session_id or None,
                    to_session_id=to,
                    nudge=True,
                    backend=target_info.get("backend") or "none",
                ok=ok,
                message=message[:100],
            )
            if ok:
                template = bridge.messages.mcp_send_ok if target_info["backend"] else bridge.messages.mcp_send_queued
                return template.format(target_prefix=target_prefix) + f" [id:{message_id}] (nudge)"
            else:
                return bridge.messages.mcp_send_failed.format(project=self._short_path(target_info["project"]))

        elif screen:
            # screen=True: require a backend and deliver via terminal multiplexer
            if not target_info["backend"]:
                bridge.audit.log(
                    "MCP_SEND",
                    message_id=message_id,
                    to_session_id=to,
                    screen=screen,
                    ok=False,
                    reason="no_backend",
                    message=message[:100],
                )
                return bridge.messages.mcp_send_no_backend.format(project=self._short_path(target_info["project"]))

            ok = await bridge._stuff_to_session(to, target_info, wrapped_message)

            if ok:
                # screen=True delivers immediately to terminal — no inbox queuing needed.
                # Inbox is reserved for nudge/screen=False (async, idle-handler pickup).
                bridge._xmpp_send(
                    json.dumps(
                        {
                            "type": "relay",
                            "mode": "screen",
                            "from": sender_session_id or None,
                            "to": to,
                            "message_id": message_id,
                            "message": message,
                            "ts": time.time(),
                        },
                        ensure_ascii=False,
                    )
                )
                bridge.audit.log(
                    "MCP_SEND",
                    message_id=message_id,
                    from_session_id=sender_session_id or None,
                    to_session_id=to,
                    screen=screen,
                    ok=True,
                    message=message[:100],
                )
                return bridge.messages.mcp_send_ok.format(target_prefix=target_prefix) + f" [id:{message_id}]"
            else:
                bridge.audit.log(
                    "MCP_SEND",
                    message_id=message_id,
                    from_session_id=sender_session_id or None,
                    to_session_id=to,
                    screen=screen,
                    ok=False,
                    reason="delivery_failed",
                    message=message[:100],
                )
                return bridge.messages.mcp_send_failed.format(project=self._short_path(target_info["project"]))
        else:
            # screen=False: only enqueue in MCP inbox, no terminal relay
            self.enqueue(to, wrapped_message, from_session=sender_session_id or None)
            bridge._xmpp_send(
                json.dumps(
                    {
                        "type": "relay",
                        "mode": "inbox",
                        "from": sender_session_id or None,
                        "to": to,
                        "message_id": message_id,
                        "message": message,
                        "ts": time.time(),
                    },
                    ensure_ascii=False,
                )
            )
            bridge.audit.log(
                "MCP_SEND",
                message_id=message_id,
                from_session_id=sender_session_id or None,
                to_session_id=to,
                screen=screen,
                ok=True,
                message=message[:100],
            )
            return bridge.messages.mcp_send_ok.format(target_prefix=target_prefix) + f" [id:{message_id}] (inbox only)"

    async def _tool_broadcast_message(self, *, message: str, sender_session_id: str, nudge: bool = False) -> str:
        """Implementation of the broadcast_message tool."""
        bridge = self._bridge
        if bridge is None:
            return "Error: bridge not initialised"
        if not message:
            return bridge.messages.broadcast_no_message

        targets = {
            sid: info
            for sid, info in bridge.registry.sessions.items()
            if sid != sender_session_id and (nudge or info.get("backend"))
        }

        if not targets:
            bridge.audit.log(
                "MCP_BROADCAST",
                from_session_id=sender_session_id or None,
                delivered=0,
                failed=0,
                message=message[:100],
            )
            return bridge.messages.broadcast_sent.format(count=0)

        wrapped_message = format_generated_agent_message(
            msg_type="broadcast",
            message=message,
            from_session_id=sender_session_id or None,
            mode="nudge" if nudge else "screen",
        )

        if nudge:
            results: list[bool] = []
            for sid, info in targets.items():
                if info.get("backend"):
                    results.append(
                        await bridge._nudge_session(
                            sid,
                            info,
                            wrapped_message,
                            from_session=sender_session_id or None,
                        )
                    )
                else:
                    self.enqueue(sid, wrapped_message, from_session=sender_session_id or None)
                    results.append(True)
        else:
            results = await asyncio.gather(
                *(bridge._stuff_to_session(sid, info, wrapped_message) for sid, info in targets.items()),
            )

        delivered = 0
        delivered_sids: list[str] = []
        for (sid, _info), ok in zip(targets.items(), results, strict=True):
            if ok:
                delivered += 1
                delivered_sids.append(sid)
            elif not nudge:
                # Screen relay failed — enqueue in MCP inbox as fallback so
                # the plugin can pick it up on the next session.idle poll.
                self.enqueue(sid, wrapped_message)

        mode = "nudge" if nudge else "screen"
        bridge._xmpp_send(
            json.dumps(
                {
                    "type": "broadcast",
                    "mode": mode,
                    "from": sender_session_id or None,
                    "to": delivered_sids,
                    "message": message,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
        )
        bridge.audit.log(
            "MCP_BROADCAST",
            from_session_id=sender_session_id or None,
            delivered=delivered,
            failed=len(targets) - delivered,
            message=message[:100],
        )
        return bridge.messages.broadcast_sent.format(count=delivered)

    def _tool_acquire_file_lock(
        self, *, session_id: str, filepath: str, project: str = "", reason: str = ""
    ) -> dict[str, Any]:
        """Implementation of ``acquire_file_lock``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "error": "bridge not initialised"}
        info = bridge.registry.get(session_id)
        if info is None:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        acquired, lock, replaced_stale = bridge.registry.acquire_file_lock(
            session_id=session_id,
            filepath=filepath,
            project=project or info["project"],
            reason=reason or None,
        )
        bridge.audit.log(
            "MCP_LOCK_ACQUIRE",
            session_id=session_id,
            filepath=filepath,
            ok=acquired,
            replaced_stale=replaced_stale,
        )
        return {"ok": acquired, "lock": dict(lock), "replaced_stale": replaced_stale}

    def _tool_release_file_lock(self, *, session_id: str, filepath: str, force: bool = False) -> dict[str, Any]:
        """Implementation of ``release_file_lock``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "released": False, "error": "bridge not initialised"}
        released = bridge.registry.release_file_lock(session_id, filepath, force=force)
        bridge.audit.log(
            "MCP_LOCK_RELEASE",
            session_id=session_id,
            filepath=filepath,
            force=force,
            released=released,
        )
        return {"ok": True, "released": released}

    def _tool_list_todos(self, *, session_id: str) -> list[dict[str, Any]]:
        """Implementation of ``list_todos``."""
        bridge = self._bridge
        if bridge is None:
            return []
        return [dict(todo) for todo in bridge.registry.list_todos(session_id)]

    def _tool_replace_todos(
        self, *, session_id: str, todos: list[dict[str, Any]], expected_version: int | None = None
    ) -> dict[str, Any]:
        """Implementation of ``replace_todos``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "error": "bridge not initialised"}
        info = bridge.registry.get(session_id)
        if info is None:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        new_version = bridge.registry.replace_todos(session_id, todos, expected_version=expected_version)
        if new_version is None:
            return {
                "ok": False,
                "error": "todo version conflict",
                "current_version": info.get("todos_version", 0),
            }
        bridge.audit.log(
            "MCP_TODOS_REPLACE",
            session_id=session_id,
            count=len(todos),
            version=new_version,
            expected_version=expected_version,
        )
        return {"ok": True, "count": len(todos), "version": new_version}

    def _tool_add_todo(
        self,
        *,
        session_id: str,
        content: str,
        status: str = "pending",
        priority: str = "medium",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Implementation of ``add_todo``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "error": "bridge not initialised"}
        info = bridge.registry.get(session_id)
        if info is None:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        todo, version = bridge.registry.add_todo(
            session_id,
            content,
            status=status,
            priority=priority,
            expected_version=expected_version,
        )
        if todo is None or version is None:
            return {
                "ok": False,
                "error": "todo version conflict",
                "current_version": info.get("todos_version", 0),
            }
        bridge.audit.log(
            "MCP_TODO_ADD",
            session_id=session_id,
            todo_id=todo["todo_id"],
            version=version,
            expected_version=expected_version,
        )
        return {"ok": True, "todo": dict(todo), "version": version}

    def _tool_update_todo(
        self,
        *,
        session_id: str,
        todo_id: str,
        content: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Implementation of ``update_todo``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "error": "bridge not initialised"}
        info = bridge.registry.get(session_id)
        if info is None:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        todo, version = bridge.registry.update_todo(
            session_id,
            todo_id,
            content=content,
            status=status,
            priority=priority,
            expected_version=expected_version,
        )
        if todo is None or version is None:
            if expected_version is not None and expected_version != info.get("todos_version", 0):
                return {
                    "ok": False,
                    "error": "todo version conflict",
                    "current_version": info.get("todos_version", 0),
                }
            return {"ok": False, "error": f"todo not found: {todo_id}"}
        bridge.audit.log(
            "MCP_TODO_UPDATE",
            session_id=session_id,
            todo_id=todo_id,
            version=version,
            expected_version=expected_version,
        )
        return {"ok": True, "todo": dict(todo), "version": version}

    def _tool_remove_todo(
        self, *, session_id: str, todo_id: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        """Implementation of ``remove_todo``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "error": "bridge not initialised"}
        info = bridge.registry.get(session_id)
        if info is None:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        removed, version = bridge.registry.remove_todo(session_id, todo_id, expected_version=expected_version)
        if version is None:
            if expected_version is not None and expected_version != info.get("todos_version", 0):
                return {
                    "ok": False,
                    "error": "todo version conflict",
                    "current_version": info.get("todos_version", 0),
                }
            return {"ok": False, "error": f"todo not found: {todo_id}"}
        bridge.audit.log(
            "MCP_TODO_REMOVE",
            session_id=session_id,
            todo_id=todo_id,
            removed=removed,
            version=version,
            expected_version=expected_version,
        )
        return {"ok": True, "removed": removed, "version": version}

    def _tool_get_session_context(self, *, session_id: str) -> dict[str, Any]:
        """Implementation of ``get_session_context``."""
        bridge = self._bridge
        if bridge is None:
            return {"ok": False, "error": "bridge not initialised"}
        info = bridge.registry.get(session_id)
        if info is None:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        return bridge._session_context_payload(session_id, info, normalize_empty=True)

    def _tool_list_file_locks(self, *, project: str = "", include_stale: bool = True) -> list[dict[str, Any]]:
        """Implementation of ``list_file_locks``."""
        bridge = self._bridge
        if bridge is None:
            locks = self._read_file_locks(project=project)
            if not include_stale:
                locks = [lock for lock in locks if not lock["stale"]]
            for lock in locks:
                lock.pop("lockfile", None)
            locks.sort(key=lambda item: (item["locked_at"], item["filepath"], item.get("source", "")))
            return locks
        return bridge._list_file_lock_payloads(project=project, include_stale=include_stale)

    def _tool_cleanup_stale_locks(self, *, project: str = "") -> dict[str, Any]:
        """Implementation of ``cleanup_stale_locks``."""
        removed: list[dict[str, Any]] = []
        bridge = self._bridge
        if bridge is not None:
            removed = bridge._cleanup_stale_lock_payloads(project=project)
        else:
            for lock in self._read_file_locks(project=project):
                if not lock["stale"]:
                    continue
                lockfile = lock.get("lockfile")
                if isinstance(lockfile, str):
                    with contextlib.suppress(OSError):
                        Path(lockfile).unlink()
                result = dict(lock)
                result.pop("lockfile", None)
                removed.append(result)

        if bridge is not None and removed:
            bridge.audit.log(
                "MCP_LOCK_CLEANUP",
                removed=len(removed),
                project=project or None,
            )
        return {"removed": len(removed), "locks": removed}

    def _tool_receive_messages(self, *, session_id: str) -> list[str]:
        """Implementation of the receive_messages tool — drains the SQLite inbox."""
        bridge = self._bridge
        if bridge is None:
            return []
        rows = bridge.registry.inbox_drain_with_senders(session_id)
        messages = [message for message, _sender in rows]
        for _message, sender_session_id in rows:
            if sender_session_id:
                bridge.registry.set_last_agent_sender(session_id, sender_session_id)
        if messages:
            bridge.audit.log(
                "MCP_RECEIVE",
                session_id=session_id,
                count=len(messages),
            )
        return messages

    async def _tool_reply_to_last_sender(self, *, session_id: str, message: str, nudge: bool = True) -> str:
        """Reply to the last remembered agent sender for *session_id*."""
        bridge = self._bridge
        if bridge is None:
            return "Error: bridge not initialised"
        sender_info = bridge.registry.get(session_id)
        if sender_info is None:
            return f"Error: unknown session_id: {session_id}"
        last_sender = bridge.registry.get_last_agent_sender(session_id)
        if not last_sender:
            return f"Error: no known sender to reply to for {session_id}"
        return await self._tool_send_message(
            to=last_sender,
            message=message,
            nudge=nudge,
            screen=not nudge,
            sender_session_id=session_id,
        )

    def _tool_list_sessions(self) -> list[dict[str, Any]]:
        """Implementation of the list_sessions tool."""
        bridge = self._bridge
        if bridge is None:
            return []
        result = []
        for sid, info in bridge.registry.sessions.items():
            result.append(bridge._session_entry(sid, info, normalize_empty=True))
        return result
