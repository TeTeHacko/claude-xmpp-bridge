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
are queued in-memory; ``receive_messages`` drains and returns them.

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
import logging
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from .bridge import XMPPBridge

log = logging.getLogger(__name__)

# Maximum number of queued messages per session before oldest are dropped.
MAX_QUEUE_SIZE = 100


class BridgeMCPServer:
    """MCP server that exposes bridge tools to OpenCode agents.

    Lifecycle:
      - Instantiated by XMPPBridge.__init__ (no I/O yet)
      - Started via ``start(bridge)`` as an asyncio task in bridge.run()
      - Stopped gracefully via ``stop()`` during bridge.shutdown()

    The ``bridge`` reference is passed to ``start()`` rather than ``__init__``
    to avoid a circular import at module level (bridge imports mcp_server, mcp_server
    would import bridge).
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._bridge: XMPPBridge | None = None
        # Per-session inbox queues.  Keys are session_id strings.
        self._queues: dict[str, asyncio.Queue[str]] = defaultdict(lambda: asyncio.Queue(maxsize=MAX_QUEUE_SIZE))
        self._task: asyncio.Task[None] | None = None
        self._mcp: FastMCP | None = None

    # ------------------------------------------------------------------
    # Public API used by XMPPBridge
    # ------------------------------------------------------------------

    def enqueue(self, session_id: str, message: str) -> None:
        """Put a message into the inbox queue for *session_id*.

        Called from bridge relay/broadcast handlers so that MCP clients
        can also receive inter-agent messages via ``receive_messages``.
        If the queue is full the oldest message is dropped to make room.
        """
        q = self._queues[session_id]
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()  # drop oldest
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(message)

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
        async def send_message(to: str, message: str, screen: bool = True) -> str:
            """Send a message to a specific agent session identified by session_id.

            By default the message is delivered via the terminal multiplexer (screen/tmux)
            exactly as the socket ``relay`` command does, AND queued in the MCP inbox.
            Set ``screen=False`` to skip terminal delivery and only queue in the MCP inbox
            (useful for testing polling without screen relay, or for sessions without a
            terminal multiplexer).

            The bridge also sends an XMPP notification to the human observer.
            The returned confirmation string includes a unique ``message_id`` that the
            recipient can reference in an ACK reply (``ack:<message_id>``).

            Args:
                to: Target session_id (as shown by list_sessions).
                message: Text to deliver to the target agent.
                screen: If True (default), deliver via screen/tmux relay.
                        If False, only enqueue in MCP inbox (no screen relay).

            Returns:
                A confirmation string with message_id on success, or an error description.
            """
            return await server._tool_send_message(to=to, message=message, screen=screen)

        @mcp.tool()
        async def broadcast_message(message: str, sender_session_id: str = "") -> str:
            """Broadcast a message to all registered agent sessions.

            The message is delivered via the terminal multiplexer to every session
            that has a backend.  The sender session (if provided) is excluded from
            delivery so an agent does not echo its own broadcast to itself.

            Args:
                message: Text to deliver to all agents.
                sender_session_id: Optional — caller's own session_id to exclude from delivery.

            Returns:
                A summary string with delivery count.
            """
            return await server._tool_broadcast_message(message=message, sender_session_id=sender_session_id)

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
        async def list_sessions() -> list[dict[str, Any]]:
            """List all currently registered agent sessions.

            Returns a list of session objects, each with:
              - session_id: unique identifier
              - project: working directory path
              - backend: multiplexer type (screen/tmux/null)
              - source: agent type (opencode/etc)
              - window: terminal window number (for screen)

            Returns:
                List of session dicts.
            """
            return server._tool_list_sessions()

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
        """Replace $HOME with ~ for display."""
        import os

        home = os.path.expanduser("~")
        return path.replace(home, "~") if path.startswith(home) else path

    async def _tool_send_message(self, *, to: str, message: str, screen: bool = True) -> str:
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

        if screen:
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

            ok = await bridge._stuff_to_session(to, target_info, message)

            if ok:
                # screen=True delivers immediately to terminal — no inbox queuing needed.
                # Inbox is reserved for screen=False (async, idle-handler pickup).
                bridge._xmpp_send(
                    f"[MCP:{message_id}] → {target_prefix}: {message[:200]}" + ("…" if len(message) > 200 else "")
                )
                bridge.audit.log(
                    "MCP_SEND",
                    message_id=message_id,
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
                    to_session_id=to,
                    screen=screen,
                    ok=False,
                    reason="delivery_failed",
                    message=message[:100],
                )
                return bridge.messages.mcp_send_failed.format(project=self._short_path(target_info["project"]))
        else:
            # screen=False: only enqueue in MCP inbox, no terminal relay
            self.enqueue(to, message)
            bridge._xmpp_send(
                f"[MCP:{message_id}] 📥 {target_prefix} (inbox only): {message[:200]}"
                + ("…" if len(message) > 200 else "")
            )
            bridge.audit.log(
                "MCP_SEND",
                message_id=message_id,
                to_session_id=to,
                screen=screen,
                ok=True,
                message=message[:100],
            )
            return bridge.messages.mcp_send_ok.format(target_prefix=target_prefix) + f" [id:{message_id}] (inbox only)"

    async def _tool_broadcast_message(self, *, message: str, sender_session_id: str) -> str:
        """Implementation of the broadcast_message tool."""
        bridge = self._bridge
        if bridge is None:
            return "Error: bridge not initialised"
        if not message:
            return bridge.messages.broadcast_no_message

        targets = {
            sid: info
            for sid, info in bridge.registry.sessions.items()
            if sid != sender_session_id and info.get("backend")
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

        results = await asyncio.gather(
            *(bridge._stuff_to_session(sid, info, message) for sid, info in targets.items()),
        )

        delivered = 0
        for (sid, _info), ok in zip(targets.items(), results, strict=True):
            if ok:
                delivered += 1
                self.enqueue(sid, message)

        sender_info = bridge.registry.get(sender_session_id) if sender_session_id else None
        sender_prefix = bridge._session_prefix(sender_info) if sender_info else (sender_session_id or "MCP")
        bridge._xmpp_send(
            f"[MCP] 📢 {sender_prefix} → {delivered} session(s): {message[:200]}" + ("…" if len(message) > 200 else "")
        )
        bridge.audit.log(
            "MCP_BROADCAST",
            from_session_id=sender_session_id or None,
            delivered=delivered,
            failed=len(targets) - delivered,
            message=message[:100],
        )
        return bridge.messages.broadcast_sent.format(count=delivered)

    def _tool_receive_messages(self, *, session_id: str) -> list[str]:
        """Implementation of the receive_messages tool — drains the inbox queue."""
        q = self._queues.get(session_id)
        if q is None:
            return []
        messages: list[str] = []
        while True:
            try:
                messages.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        bridge = self._bridge
        if bridge is not None and messages:
            bridge.audit.log(
                "MCP_RECEIVE",
                session_id=session_id,
                count=len(messages),
            )
        return messages

    def _tool_list_sessions(self) -> list[dict[str, Any]]:
        """Implementation of the list_sessions tool."""
        bridge = self._bridge
        if bridge is None:
            return []
        result = []
        for sid, info in bridge.registry.sessions.items():
            result.append(
                {
                    "session_id": sid,
                    "project": info["project"],
                    "backend": info.get("backend") or "null",
                    "source": info.get("source") or "",
                    "window": info.get("window") or "",
                    "sty": info.get("sty") or "",
                }
            )
        return result
