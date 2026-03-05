"""Main bridge daemon — orchestrator composing all components."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
import time
from pathlib import Path

import slixmpp

from .audit import AuditLogger
from .config import Config
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

# Allowed values for the 'source' field
_VALID_SOURCES: frozenset[str | None] = frozenset({"opencode", None})

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

    # --- XMPP message handling ---

    async def _on_xmpp_message(self, msg: slixmpp.Message) -> None:
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
            icon = "🧠" if source == "opencode" else "⚡"
            window_label = self._window_label(info)
            if backend == "screen":
                tag = f"[{icon}screen{window_label}]"
            elif backend == "tmux":
                tag = f"[{icon}tmux{window_label}]"
            else:
                tag = f"[{icon}{self.messages.read_only_tag}]"
            project = info["project"]
            lines.append(f"  /{i} {self._short_path(project)} {tag}{marker}")
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

    def _xmpp_send(self, text: str) -> bool:
        return self.xmpp.send(self.config.recipient, text)

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

    def _session_prefix(self, info: SessionInfo) -> str:
        """Return icon + bracketed project + window label, e.g. '⚡[~/foo #2]'."""
        icon = "🧠" if info.get("source") == "opencode" else "⚡"
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
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            sty,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
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
        cmd = str(req.get("cmd", ""))
        session_id = str(req.get("session_id", "")) or None

        response: dict[str, object]
        if cmd == "register":
            response = self._handle_register(req)
        elif cmd == "unregister":
            response = self._handle_unregister(req)
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
        elif cmd == "query":
            response = self._handle_query(req)
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
            if source not in _VALID_SOURCES:
                return {"error": f"unsupported source: {source!r}"}

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
        Falls back to a plain message when the session is not found.
        """
        session_id = str(req.get("session_id", ""))
        message = str(req.get("message", ""))
        if not message:
            return {"ok": True}
        info = self.registry.get(session_id)
        prefix = self._session_prefix(info) if info else ""
        full_msg = f"{prefix} {message}".strip() if prefix else message
        self._xmpp_send(full_msg)
        return {"ok": True}

    def _handle_query(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        if not session_id:
            return {"error": "missing session_id"}
        info = self.registry.get(session_id)
        if not info:
            return {"error": "session not found"}
        return {"ok": True, "project": info["project"]}

    # --- Lifecycle ---

    async def run(self) -> None:
        """Start all components and wait for shutdown signal."""
        await self._cleanup_stale_sessions()
        self.xmpp.start()
        await self.socket_server.start()

        stop = asyncio.Event()

        def _signal_handler() -> None:
            log.info("Shutdown signal received")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        await self.xmpp.connected.wait()
        self.audit.log("BRIDGE_START", jid=self.config.jid, recipient=self.config.recipient)
        self._xmpp_send(self.messages.bridge_started)
        log.info("Bridge running. Press Ctrl+C to stop.")

        await stop.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        """Gracefully shut down all components."""
        self.audit.log("BRIDGE_STOP", jid=self.config.jid)
        log.info("Shutting down...")
        await self.socket_server.stop()
        self._xmpp_send(self.messages.bridge_stopped)
        await asyncio.sleep(1)
        self.xmpp.disconnect()
        self.registry.close()
        self.audit.close()
        log.info("Bye.")
