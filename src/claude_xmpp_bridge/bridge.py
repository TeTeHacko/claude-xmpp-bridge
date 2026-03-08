"""Main bridge daemon — orchestrator composing all components."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import time
from pathlib import Path

import slixmpp

from . import __version__
from .audit import AuditLogger
from .config import DEFAULT_SOURCE_ICONS, MAX_SOURCE_LEN, Config
from .mcp_server import BridgeMCPServer
from .messages import Messages, load_messages
from .multiplexer import get_multiplexer
from .registry import SessionInfo, SessionRegistry
from .socket_server import SocketServer
from .xmpp import XMPPConnection

log = logging.getLogger(__name__)

# Security limits
MAX_SESSIONS = 50  # max registered sessions per bridge instance
MAX_PROJECT_LEN = 4096  # max length of project path
MAX_XMPP_BODY = 10_000  # max length of incoming XMPP message body (chars)

# Sessions registered without a terminal multiplexer (backend=None) are useful
# for sending notifications but cannot receive messages.  They are automatically
# expired after this many seconds so stale entries don't accumulate.
NO_BACKEND_SESSION_TTL = 24 * 3600  # 24 hours

_ALIVE_CHECK_CMDS: dict[str, list[str]] = {
    "screen": ["screen", "-ls"],
    "tmux": ["tmux", "has-session", "-t"],
}


@dataclasses.dataclass
class _PendingAsk:
    """A queued ask waiting for a human reply via XMPP."""

    message: str
    future: asyncio.Future[str]


class XMPPBridge:
    """Orchestrator that composes XMPP, socket server, registry, and multiplexer."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.messages: Messages = load_messages(config.messages_file)
        self.registry = SessionRegistry(config.db_path)
        self.xmpp = XMPPConnection(config.jid, config.password, force_starttls=config.force_starttls)
        self.xmpp.on_message(self._on_xmpp_message)
        self.audit = AuditLogger(config.audit_log)
        self.socket_server = SocketServer(
            config.socket_path,
            self._handle_request,
            config.socket_token,
            audit_logger=self.audit,
        )
        self._ask_queue: list[_PendingAsk] = []
        self.mcp_server: BridgeMCPServer | None = BridgeMCPServer(config.mcp_port) if config.mcp_port else None

    # --- XMPP message handling ---

    async def _on_xmpp_message(self, msg: slixmpp.Message) -> None:
        """Route an incoming XMPP message from the authorized sender.

        Priority: (1) if an ask is pending and the message is not a command,
        deliver it as the ask reply; (2) commands starting with '/' are
        dispatched to _handle_command; (3) plain text is sent to the active
        terminal session via _send_to_session.  Messages from unauthorized
        senders or non-chat types are silently dropped.
        """
        if msg["type"] not in ("chat", "normal"):
            return
        sender = msg["from"].bare
        if sender != self.config.recipient:
            log.warning("Ignored XMPP message from unexpected sender: %s", sender)
            self.audit.log("XMPP_REJECTED", from_jid=sender, reason="unauthorized_sender")
            return

        body: str = msg["body"].strip()[:MAX_XMPP_BODY]
        if not body:
            return

        log.info("XMPP message: %s", body[:80])

        # If there is a pending ask, the incoming message is the answer
        if self._ask_queue and not body.startswith("/"):
            pending = self._ask_queue[0]
            if not pending.future.done():
                pending.future.set_result(body)
            self.audit.log(
                "XMPP_IN", from_jid=sender, allowed=True, body=body, body_len=len(body), routed_to="ask_reply"
            )
            return

        if body.startswith("/"):
            self.audit.log("XMPP_IN", from_jid=sender, allowed=True, body=body, body_len=len(body), routed_to="command")
            await self._handle_command(body)
        else:
            self.audit.log("XMPP_IN", from_jid=sender, allowed=True, body=body, body_len=len(body), routed_to="session")
            await self._send_to_session(None, body)

    async def _handle_command(self, body: str) -> None:
        parts = body.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/list", "/l"):
            await self._cmd_list()
        elif cmd == "/help":
            self._xmpp_send(self.messages.help_text)
        elif cmd.startswith("/") and cmd[1:].isdigit():
            index = int(cmd[1:])
            if arg:
                await self._send_to_session_by_index(index, arg)
            else:
                self._xmpp_send(self.messages.usage_send_to.format(cmd=cmd))
        else:
            self._xmpp_send(self.messages.unknown_command.format(cmd=cmd))

    async def _cmd_list(self) -> None:
        await self._cleanup_stale_sessions()
        sessions = self.registry.list_sessions()
        if not sessions:
            self._xmpp_send(self.messages.no_sessions)
            return

        lines = [self.messages.session_list_header]
        sorted_ids = sorted(sessions, key=lambda s: sessions[s]["registered_at"])
        active_id = self.registry.last_active
        for i, sid in enumerate(sorted_ids, 1):
            info = sessions[sid]
            marker = " *" if sid == active_id else ""
            backend = info["backend"]
            source = info.get("source")
            icon = self._source_icon(source)
            window_label = self._window_label(info)
            if backend == "screen":
                tag = f"[{icon}screen{window_label}]"
            elif backend == "tmux":
                tag = f"[{icon}tmux{window_label}]"
            else:
                tag = f"[{icon}{self.messages.read_only_tag}]"
            project = info["project"]
            state = info.get("agent_state") or ""
            version = info.get("plugin_version") or ""
            meta = ""
            if state:
                state_icon = "⏸" if state == "idle" else "▶" if state == "running" else state
                meta += f" {state_icon}"
            if version:
                meta += f" v{version}"
            lines.append(f"  /{i} {self._short_path(project)} {tag}{meta}{marker}")
        lines.append(f"\n{self.messages.active_marker}")
        self._xmpp_send("\n".join(lines))

    async def _send_to_session_by_index(self, index: int, text: str) -> None:
        sid, info = self.registry.get_by_index(index)
        if not info:
            self._xmpp_send(self.messages.session_not_found.format(index=index))
            return
        if sid is None:
            return
        self.registry.set_active(sid)
        prefix = self._session_prefix(info)
        if not info["backend"]:
            self._xmpp_send(self.messages.no_backend.format(project=self._short_path(info["project"])))
            return
        ok = await self._stuff_to_session(sid, info, text)
        if ok:
            if not self._xmpp_send(f"→ {prefix} {self.messages.sent}"):
                log.warning("Sent to session but XMPP confirmation failed (project=%s)", info["project"])
        else:
            self._xmpp_send(self.messages.delivery_failed.format(project=self._short_path(info["project"])))

    async def _send_to_session(self, session_id: str | None, text: str) -> None:
        if session_id:
            info = self.registry.get(session_id)
            if not info:
                self._xmpp_send(self.messages.no_active_session)
                return
            self.registry.set_active(session_id)
        else:
            session_id, info = self.registry.get_active()
            if not info:
                self._xmpp_send(self.messages.no_active_session)
                return

        # session_id is guaranteed non-None when info is not None
        prefix = self._session_prefix(info)
        if not info["backend"]:
            self._xmpp_send(self.messages.no_backend.format(project=self._short_path(info["project"])))
            return
        ok = await self._stuff_to_session(session_id, info, text)  # type: ignore[arg-type]
        if ok:
            if not self._xmpp_send(f"→ {prefix} {self.messages.sent}"):
                log.warning("Sent to session but XMPP confirmation failed (project=%s)", info["project"])
        else:
            self._xmpp_send(self.messages.delivery_failed.format(project=self._short_path(info["project"])))

    async def _stuff_to_session(self, session_id: str, info: SessionInfo, text: str) -> bool:
        mux = get_multiplexer(info["backend"])
        if not mux:
            log.warning("No backend for session (project=%s)", info["project"])
            return False
        ok = await mux.send_text(info["sty"], info["window"], text)
        if ok:
            self.audit.log(
                "TERMINAL_SEND",
                session_id=session_id,
                project=info["project"],
                backend=info["backend"],
                text=text,
                text_len=len(text),
            )
        else:
            self.audit.log(
                "TERMINAL_SEND_FAILED",
                session_id=session_id,
                project=info["project"],
                backend=info["backend"],
                text=text,
                text_len=len(text),
            )
        return ok

    async def _nudge_session(self, session_id: str, info: SessionInfo, message: str) -> bool:
        """Enqueue *message* to the MCP inbox and send a bare CR nudge to the session.

        The nudge pattern (Návrh #3) separates message delivery from terminal
        injection: the full message is stored safely in SQLite, and only a CR
        is sent via the multiplexer.  This causes readline/session.idle to fire
        in the target agent's plugin, which then calls receive_messages() to
        drain the inbox — completely avoiding the race condition where a screen
        inject arrives while the agent is busy processing tool calls.

        Returns True if the CR nudge was delivered successfully (the inbox write
        always succeeds — it is the nudge delivery that can fail).
        """
        mux = get_multiplexer(info["backend"])
        if not mux:
            log.warning("No backend for nudge (project=%s)", info["project"])
            return False
        self._enqueue_for_mcp(session_id, message)
        ok = await mux.send_nudge(info["sty"], info["window"])
        if ok:
            self.audit.log(
                "NUDGE_SENT",
                session_id=session_id,
                project=info["project"],
                backend=info["backend"],
                message=message,
                message_len=len(message),
            )
        else:
            self.audit.log(
                "NUDGE_FAILED",
                session_id=session_id,
                project=info["project"],
                backend=info["backend"],
                message=message,
                message_len=len(message),
            )
        return ok

    def _xmpp_send(self, text: str) -> bool:
        return self.xmpp.send(self.config.recipient, text)

    def _enqueue_for_mcp(self, session_id: str, message: str) -> None:
        """Queue *message* into the MCP inbox for *session_id* (no-op if MCP disabled)."""
        if self.mcp_server is not None:
            self.mcp_server.enqueue(session_id, message)

    @staticmethod
    def _short_path(path: str) -> str:
        """Shorten path by replacing home directory with ~."""
        home = str(Path.home())
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~" + path[len(home) :]
        return path

    @staticmethod
    def _window_label(info: SessionInfo) -> str:
        """Return a compact window/pane identifier string, e.g. ' #2' or ' :%3'."""
        backend = info.get("backend")
        window = info.get("window", "")
        sty = info.get("sty", "")
        if backend == "screen" and window:
            return f" #{window}"
        if backend == "tmux" and sty:
            # sty holds TMUX_PANE which has the form "%3"
            return f" :{sty}"
        return ""

    def _source_icon(self, source: str | None) -> str:
        """Return the icon for a given source string.

        Lookup order:
          1. Per-instance config (from [source_icons] TOML section)
          2. Built-in DEFAULT_SOURCE_ICONS
          3. Hardcoded fallback "⚡"
        """
        icons = {**DEFAULT_SOURCE_ICONS, **self.config.source_icons}
        return icons.get(source) or icons.get(None, "⚡")

    def _session_prefix(self, info: SessionInfo) -> str:
        """Return icon + bracketed project + window label, e.g. '⚡[~/foo #2]'."""
        icon = self._source_icon(info.get("source"))
        project = self._short_path(info["project"])
        loc = self._window_label(info)
        return f"{icon}[{project}{loc}]"

    # --- Stale session cleanup ---

    async def _is_session_alive(self, info: SessionInfo) -> bool:
        """Check if the session's terminal multiplexer is still running."""
        backend = info["backend"]
        if backend is None:
            return True  # read-only sessions can't be verified
        cmd_parts = _ALIVE_CHECK_CMDS.get(backend)
        if cmd_parts is None:
            return True  # unknown backend — assume alive
        sty = info["sty"]
        if not sty:
            return True  # no sty — can't check

        env = {}
        for var in ("PATH", "USER", "HOME"):
            if var in os.environ:
                env[var] = os.environ[var]

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            sty,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False
        return proc.returncode == 0

    async def _cleanup_stale_sessions(self) -> int:
        """Remove dead sessions and deduplicate (keep newest per key)."""
        to_remove: dict[str, str] = {}  # sid -> reason

        # 1. Remove sessions whose multiplexer is dead (check in parallel)
        items = list(self.registry.sessions.items())
        if items:
            alive_results = await asyncio.gather(*(self._is_session_alive(info) for _, info in items))
            for (sid, _), alive in zip(items, alive_results, strict=True):
                if not alive:
                    to_remove[sid] = "dead"

        # 2. Expire no-backend sessions older than NO_BACKEND_SESSION_TTL.
        now = time.time()
        for sid, info in list(self.registry.sessions.items()):
            if sid in to_remove:
                continue
            if info["backend"] is None and (now - info["registered_at"]) > NO_BACKEND_SESSION_TTL:
                age_h = (now - info["registered_at"]) / 3600
                to_remove[sid] = "expired"
                log.info("Expiring no-backend session %s (age=%.0fh)", sid, age_h)

        # 3. Deduplicate — keep only the newest per multiplexer slot (sty+window for screen,
        # sty/pane for tmux). Multiple instances in different windows of the same project are
        # kept alive; only entries sharing the exact same terminal slot are collapsed.
        groups: dict[object, list[tuple[str, float]]] = {}
        for sid, info in list(self.registry.sessions.items()):
            if sid in to_remove:
                continue
            key = (info["sty"], info["window"]) if info["sty"] else None
            if key is None:
                continue
            groups.setdefault(key, []).append((sid, info["registered_at"]))
        for entries in groups.values():
            if len(entries) > 1:
                entries.sort(key=lambda e: e[1])  # oldest first
                for sid, _ in entries[:-1]:  # remove all but newest
                    to_remove[sid] = "duplicate"

        for sid, reason in to_remove.items():
            stale = self.registry.sessions.get(sid)
            project = stale["project"] if stale else "?"
            self.registry.unregister(sid)
            log.info("Cleaned stale session %s (project=%s, reason=%s)", sid, project, reason)
            self.audit.log("SESSION_EXPIRED", session_id=sid, project=project, reason=reason)
        if to_remove:
            log.info(self.messages.stale_sessions_cleaned.format(count=len(to_remove)))
        return len(to_remove)

    # --- Ask queue ---

    def _send_next_ask(self) -> None:
        """Send the XMPP message for the first pending ask in the queue."""
        if self._ask_queue:
            pending = self._ask_queue[0]
            self._xmpp_send(pending.message)
            log.info("Ask sent: %s", pending.message[:100])

    # Maximum allowed ask timeout (seconds). Prevents a compromised local process
    # from permanently blocking the ask queue with an arbitrarily large timeout.
    MAX_ASK_TIMEOUT = 3600  # 1 hour

    async def _handle_ask(self, req: dict[str, object]) -> dict[str, object]:
        """Queue an ask, send via XMPP (FIFO), wait for reply or timeout."""
        message = str(req.get("message", ""))
        if not message:
            return {"ok": False, "error": "missing message"}
        try:
            timeout = int(str(req.get("timeout", 300)))
        except (ValueError, TypeError):
            return {"ok": False, "error": "timeout must be an integer"}
        if timeout <= 0 or timeout > self.MAX_ASK_TIMEOUT:
            return {"ok": False, "error": f"timeout must be between 1 and {self.MAX_ASK_TIMEOUT}"}

        # Tag the message with session prefix if available
        session_id = str(req.get("session_id", ""))
        if session_id:
            info = self.registry.get(session_id)
            if info:
                prefix = self._session_prefix(info)
                message = f"❓{prefix} {message}"
            else:
                message = f"❓ {message}"
        else:
            message = f"❓ {message}"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        pending = _PendingAsk(message=message, future=future)
        self._ask_queue.append(pending)

        self.audit.log(
            "ASK_QUEUED",
            session_id=session_id or None,
            message=message,
            message_len=len(message),
            timeout=timeout,
            queue_depth=len(self._ask_queue),
        )

        # If this is the only item, send immediately
        if len(self._ask_queue) == 1:
            self._send_next_ask()

        t0 = time.monotonic()
        try:
            reply = await asyncio.wait_for(future, timeout=timeout)
            elapsed = round(time.monotonic() - t0, 3)
            self.audit.log(
                "ASK_ANSWERED",
                session_id=session_id or None,
                elapsed_s=elapsed,
                reply=reply,
                reply_len=len(reply),
            )
            return {"ok": True, "reply": reply}
        except TimeoutError:
            elapsed = round(time.monotonic() - t0, 3)
            self.audit.log(
                "ASK_TIMEOUT",
                session_id=session_id or None,
                elapsed_s=elapsed,
                timeout=timeout,
            )
            return {"ok": False, "error": "timeout"}
        finally:
            # Remove from queue and send next
            if pending in self._ask_queue:
                self._ask_queue.remove(pending)
            self._send_next_ask()

    # --- Socket request handling ---

    async def _handle_request(self, req: dict[str, object]) -> dict[str, object]:
        """Dispatch a JSON request received over the Unix socket.

        Supported commands: register, unregister, send, notify, response,
        ask, query.  Each command is delegated to its _handle_* method.
        Every request (and its outcome) is recorded as a SOCKET_CMD audit event.
        """
        cmd = str(req.get("cmd", ""))
        session_id = str(req.get("session_id", "")) or None

        response: dict[str, object]
        if cmd == "register":
            response = self._handle_register(req)
        elif cmd == "unregister":
            response = self._handle_unregister(req)
        elif cmd == "state":
            response = self._handle_state(req)
        elif cmd == "send":
            message = str(req.get("message", ""))
            if message:
                sent = self._xmpp_send(message)
                response = {"ok": sent}
            else:
                response = {"ok": True}
        elif cmd == "notify":
            response = self._handle_notify(req)
        elif cmd == "response":
            response = self._handle_response(req)
        elif cmd == "ask":
            response = await self._handle_ask(req)
        elif cmd == "relay":
            response = await self._handle_relay(req)
        elif cmd == "broadcast":
            response = await self._handle_broadcast(req)
        elif cmd == "query":
            response = self._handle_query(req)
        elif cmd == "list":
            response = self._handle_list(req)
        elif cmd == "ping":
            response = {"ok": True}
        else:
            response = {"error": f"unknown command: {cmd}"}

        self.audit.log(
            "SOCKET_CMD",
            cmd=cmd,
            session_id=session_id,
            ok="ok" in response and bool(response["ok"]),
            error=response.get("error"),
        )
        return response

    def _handle_register(self, req: dict[str, object]) -> dict[str, object]:
        """Register (or re-register) a coding session with the bridge.

        Validates all fields (session_id, sty, window, project, backend, source),
        deduplicates by multiplexer slot (same sty+window replaces old entry while
        inheriting its registered_at timestamp to keep stable /list ordering), and
        enforces the MAX_SESSIONS limit.  Multiple instances of the same project in
        different terminal windows are intentionally allowed.
        """
        try:
            sid = str(req.get("session_id", ""))
            if not sid:
                return {"error": "missing session_id"}
            sty = str(req.get("sty", ""))
            window = str(req.get("window", ""))
            backend_raw = req.get("backend")
            if backend_raw is None:
                backend = "screen" if sty else None
            elif str(backend_raw) == "none":
                backend = None
            else:
                backend = str(backend_raw)
                if backend not in ("screen", "tmux"):
                    return {"error": f"unsupported backend: {backend}"}

            project = str(req.get("project", ""))
            if not project:
                return {"error": "missing project"}
            if len(project) > MAX_PROJECT_LEN:
                return {"error": f"project path too long (max {MAX_PROJECT_LEN} chars)"}

            source_raw = req.get("source")
            source = str(source_raw) if source_raw is not None else None
            if source is not None and len(source) > MAX_SOURCE_LEN:
                return {"error": f"source too long (max {MAX_SOURCE_LEN} chars)"}

            plugin_version_raw = req.get("plugin_version")
            plugin_version = str(plugin_version_raw) if plugin_version_raw is not None else None

            # Deduplicate: remove old sessions occupying the same multiplexer slot (sty+window
            # for screen, or sty/pane for tmux). This handles restarts inside the same terminal
            # window without accumulating ghost entries.
            #
            # We intentionally do NOT deduplicate by (project, source) — multiple instances of
            # the same agent running in different windows of the same project are all welcome.
            # Stale dead sessions from the same project are cleaned up lazily by
            # _cleanup_stale_sessions() (called on /list).
            inherited_registered_at: float | None = None
            for old_sid, old_info in list(self.registry.sessions.items()):
                if old_sid == sid:
                    continue
                if (
                    sty
                    and old_info["sty"] == sty
                    and ((backend == "screen" and window and old_info["window"] == window) or backend == "tmux")
                ):
                    if inherited_registered_at is None:
                        inherited_registered_at = old_info["registered_at"]
                    log.info("Replacing stale session %s (same sty=%s, window=%s)", old_sid, sty, window)
                    self.audit.log(
                        "SESSION_REPLACED",
                        old_session_id=old_sid,
                        new_session_id=sid,
                        sty=sty,
                        window=window,
                        project=project,
                    )
                    self.registry.unregister(old_sid)

            # Check session limit (after deduplication so replacements don't count)
            if sid not in self.registry.sessions and len(self.registry.sessions) >= MAX_SESSIONS:
                self.audit.log(
                    "SESSION_LIMIT_HIT",
                    session_id=sid,
                    project=project,
                    limit=MAX_SESSIONS,
                    current=len(self.registry.sessions),
                )
                return {"error": f"session limit reached (max {MAX_SESSIONS})"}

            self.registry.register(
                session_id=sid,
                sty=sty,
                window=window,
                project=project,
                backend=backend,
                source=source,
                registered_at=inherited_registered_at,
                plugin_version=plugin_version,
            )
            self.audit.log(
                "SESSION_REGISTERED",
                session_id=sid,
                sty=sty,
                window=window,
                project=project,
                backend=backend,
                source=source,
            )
        except KeyError as e:
            return {"error": f"missing field: {e}"}
        except ValueError as e:
            return {"error": str(e)}
        return {"ok": True}

    def _handle_unregister(self, req: dict[str, object]) -> dict[str, object]:
        sid = req.get("session_id")
        if not sid:
            return {"error": "missing session_id"}
        sid_str = str(sid)
        info = self.registry.get(sid_str)
        self.registry.unregister(sid_str)
        self.audit.log(
            "SESSION_UNREGISTERED",
            session_id=sid_str,
            project=info["project"] if info else None,
        )
        return {"ok": True}

    def _handle_state(self, req: dict[str, object]) -> dict[str, object]:
        """Update the agent_state for a registered session.

        Protocol fields:
          - ``session_id`` : session to update (required)
          - ``state``      : new state string, e.g. "idle" or "running" (required)
        """
        sid = str(req.get("session_id", ""))
        if not sid:
            return {"error": "missing session_id"}
        state = str(req.get("state", ""))
        if not state:
            return {"error": "missing state"}
        updated = self.registry.update_state(sid, state)
        if not updated:
            return {"error": f"session not found: {sid}"}
        self.audit.log("SESSION_STATE", session_id=sid, state=state)
        return {"ok": True}

    def _handle_response(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        message = str(req.get("message", ""))
        if message:
            info = self.registry.get(session_id)
            if info:
                prefix = self._session_prefix(info)
            elif "project" in req:
                prefix = f"[{self._short_path(str(req['project']))}]"
            else:
                prefix = "[?]"
            self._xmpp_send(f"{prefix} {message}")
        return {"ok": True}

    def _handle_notify(self, req: dict[str, object]) -> dict[str, object]:
        """Send a session-tagged XMPP notification from a hook.

        Looks up the session in the registry to obtain the icon, project path
        and window/pane identifier so every outgoing message is clearly labelled.
        If the session is not found (e.g. race condition on startup) but the
        request contains a ``source`` hint, the correct icon is still used.
        Falls back to a plain message when neither session nor source is available.
        """
        session_id = str(req.get("session_id", ""))
        message = str(req.get("message", ""))
        if not message:
            return {"ok": True}
        info = self.registry.get(session_id)
        if info:
            prefix = self._session_prefix(info)
        else:
            # Session not in registry yet — build minimal prefix from request fields.
            source_raw = req.get("source")
            source = str(source_raw) if source_raw is not None else None
            icon = self._source_icon(source)
            project_raw = req.get("project", "")
            if project_raw:  # noqa: SIM108 — ternary would be unreadable here
                prefix = f"{icon}[{self._short_path(str(project_raw))}]"
            else:
                prefix = icon if icon else ""
        full_msg = f"{prefix} {message}".strip() if prefix else message
        self._xmpp_send(full_msg)
        return {"ok": True}

    def _find_session_by_project(self, project: str) -> tuple[str | None, SessionInfo | None]:
        """Find a session whose project path starts with the given prefix.

        Expands a leading ``~`` to the home directory before matching.
        Returns the first match sorted by registration time, or (None, None)
        if no session matches.
        """
        home = str(Path.home())
        needle = project.replace("~", home, 1) if project.startswith("~") else project
        sessions = self.registry.list_sessions()
        sorted_ids = sorted(sessions, key=lambda s: sessions[s]["registered_at"])
        for sid in sorted_ids:
            info = sessions[sid]
            if info["project"].startswith(needle):
                return sid, info
        return None, None

    async def _handle_relay(self, req: dict[str, object]) -> dict[str, object]:
        """Deliver a message from one agent to another via the terminal multiplexer.

        The sender identifies itself with ``session_id`` (optional but recommended
        for the XMPP observer notification).  The target is specified via either
        ``to`` (a session_id string) or ``to_index`` (a 1-based integer matching
        ``/list`` order).  The bridge stuffs the message into the target terminal
        and sends an XMPP notification so the human observer can follow along.

        Protocol fields:
          - ``message``    : text to send to the target agent (required)
          - ``to``         : target session_id string           (mutually exclusive)
          - ``to_index``   : target session index (1-based int) (mutually exclusive)
          - ``to_project`` : target project path prefix         (mutually exclusive)
          - ``session_id`` : sender session_id for labelling    (optional)
          - ``nudge``      : if True, store in inbox + send CR only (default: False)
        """
        message = str(req.get("message", ""))
        if not message:
            return {"ok": False, "error": "missing message"}

        nudge = bool(req.get("nudge", False))

        # Resolve target session
        to_raw = req.get("to")
        to_index_raw = req.get("to_index")
        to_project_raw = req.get("to_project")
        if to_raw is not None:
            target_id = str(to_raw)
            target_info = self.registry.get(target_id)
        elif to_index_raw is not None:
            try:
                to_index = int(str(to_index_raw))
            except (ValueError, TypeError):
                return {"ok": False, "error": "to_index must be an integer"}
            target_id, target_info = self.registry.get_by_index(to_index)  # type: ignore[assignment]
        elif to_project_raw is not None:
            target_id, target_info = self._find_session_by_project(str(to_project_raw))  # type: ignore[assignment]
        else:
            return {"ok": False, "error": self.messages.relay_no_target}

        if not target_info or not target_id:
            return {"ok": False, "error": self.messages.relay_target_not_found}

        if not target_info["backend"]:
            return {
                "ok": False,
                "error": self.messages.relay_no_backend.format(project=self._short_path(target_info["project"])),
            }

        # Build XMPP observer label
        sender_id = str(req.get("session_id", ""))
        sender_info = self.registry.get(sender_id) if sender_id else None
        sender_prefix = self._session_prefix(sender_info) if sender_info else (sender_id or "?")
        target_prefix = self._session_prefix(target_info)

        if nudge:
            ok = await self._nudge_session(target_id, target_info, message)
        else:
            ok = await self._stuff_to_session(target_id, target_info, message)

        self.audit.log(
            "RELAY_SENT" if ok else "RELAY_FAILED",
            from_session_id=sender_id or None,
            to_session_id=target_id,
            message=message,
            message_len=len(message),
        )

        mode = "nudge" if nudge else "screen"
        if ok:
            # Notify observer so they can see inter-agent traffic
            self._xmpp_send(
                f"🤖 {sender_prefix} ──{mode}──▶ {target_prefix}\n  {message[:200]}"
                + ("…" if len(message) > 200 else "")
            )
            return {"ok": True}
        else:
            return {
                "ok": False,
                "error": self.messages.relay_failed.format(project=self._short_path(target_info["project"])),
            }

    async def _handle_broadcast(self, req: dict[str, object]) -> dict[str, object]:
        """Deliver a message to all sessions except the sender.

        The sender is identified by ``session_id`` (excluded from delivery so
        an agent does not echo its own broadcast back to itself).  The bridge
        stuffs the message into each target terminal and sends one XMPP summary
        with the list of recipients so the human observer has a complete picture.

        Protocol fields:
          - ``message``    : text to send to all other agents (required)
          - ``session_id`` : sender session_id (excluded from delivery, optional)
          - ``nudge``      : if True, store in inbox + send CR only (default: False)
        """
        message = str(req.get("message", ""))
        if not message:
            return {"ok": False, "error": self.messages.broadcast_no_message}

        nudge = bool(req.get("nudge", False))

        sender_id = str(req.get("session_id", ""))
        sender_info = self.registry.get(sender_id) if sender_id else None
        sender_prefix = self._session_prefix(sender_info) if sender_info else (sender_id or "?")

        targets = {
            sid: info for sid, info in self.registry.sessions.items() if sid != sender_id and info.get("backend")
        }

        if not targets:
            return {"ok": True, "delivered": 0}

        if nudge:
            results = await asyncio.gather(
                *(self._nudge_session(sid, info, message) for sid, info in targets.items()),
            )
        else:
            results = await asyncio.gather(
                *(self._stuff_to_session(sid, info, message) for sid, info in targets.items()),
            )

        delivered: list[str] = []
        failed: list[str] = []
        for (_sid, info), ok in zip(targets.items(), results, strict=True):
            if ok:
                delivered.append(self._session_prefix(info))
                if not nudge:
                    # Screen relay succeeded — no MCP inbox needed.
                    # Enqueueing here would cause double-delivery via pollInbox().
                    pass
            else:
                failed.append(self._session_prefix(info))
                if not nudge:
                    # Screen relay failed — enqueue as fallback for pollInbox().
                    self._enqueue_for_mcp(_sid, message)

        self.audit.log(
            "BROADCAST_SENT",
            from_session_id=sender_id or None,
            delivered=len(delivered),
            failed=len(failed),
            message=message,
            message_len=len(message),
        )

        # Single XMPP summary for the observer
        mode = "nudge" if nudge else "screen"
        self._xmpp_send(
            f"🤖 {sender_prefix} ──{mode}──▶▶ {len(delivered)} session(s)\n  {message[:200]}"
            + ("…" if len(message) > 200 else "")
        )

        return {"ok": True, "delivered": len(delivered), "failed": len(failed)}

    def _handle_query(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        if not session_id:
            return {"error": "missing session_id"}
        info = self.registry.get(session_id)
        if not info:
            return {"error": "session not found"}
        return {"ok": True, "project": info["project"]}

    def _handle_list(self, req: dict[str, object]) -> dict[str, object]:
        """Return all registered sessions as a list.

        Useful for agents that need to discover other sessions without
        knowing their session_id in advance.  Each entry includes the
        session_id, project path, backend, window and source fields so
        callers can pick the right target for a subsequent relay.

        Protocol response fields:
          - ``ok``       : True
          - ``sessions`` : list of dicts with session_id, project, backend,
                           sty, window, source, registered_at
        """
        sessions = self.registry.list_sessions()
        sorted_ids = sorted(sessions, key=lambda s: sessions[s]["registered_at"])
        result = []
        for i, sid in enumerate(sorted_ids, 1):
            info = sessions[sid]
            result.append(
                {
                    "index": i,
                    "session_id": sid,
                    "project": info["project"],
                    "backend": info["backend"],
                    "sty": info.get("sty", ""),
                    "window": info["window"],
                    "source": info.get("source"),
                    "registered_at": info["registered_at"],
                    "plugin_version": info.get("plugin_version"),
                    "agent_state": info.get("agent_state"),
                }
            )
        return {"ok": True, "sessions": result}

    # --- Lifecycle ---

    async def run(self) -> None:
        """Start all components and wait for shutdown signal."""
        await self._cleanup_stale_sessions()
        self.xmpp.start()
        await self.socket_server.start()

        if self.mcp_server is not None:
            await self.mcp_server.start(self)

        stop = asyncio.Event()

        def _signal_handler() -> None:
            log.info("Shutdown signal received")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        await self.xmpp.connected.wait()
        self.audit.log("BRIDGE_START", jid=self.config.jid, recipient=self.config.recipient)
        startup_msg = f"{self.messages.bridge_started} (v{__version__})"
        if self.mcp_server is not None:
            startup_msg += f" {self.messages.mcp_started.format(port=self.mcp_server.port)}"
        self._xmpp_send(startup_msg)
        log.info("Bridge running. Press Ctrl+C to stop.")

        heartbeat_task = asyncio.create_task(self._heartbeat(stop))
        await stop.wait()
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await self.shutdown()

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        """Periodically check registered sessions and remove stale ones.

        Runs every 60 seconds. A session is considered stale when its Screen
        window no longer exists (the screen -Q select command exits non-zero).
        tmux sessions are checked via ``tmux has-session``.  Sessions with no
        backend are always kept (read-only observer sessions).
        """
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=60)
            if stop.is_set():
                break
            await self._cleanup_stale_sessions()

    async def shutdown(self) -> None:
        """Gracefully shut down all components."""
        self.audit.log("BRIDGE_STOP", jid=self.config.jid)
        log.info("Shutting down...")
        if self.mcp_server is not None:
            await self.mcp_server.stop()
        await self.socket_server.stop()
        self._xmpp_send(self.messages.bridge_stopped)
        await asyncio.sleep(1)
        self.xmpp.disconnect()
        self.registry.close()
        self.audit.close()
        log.info("Bye.")
