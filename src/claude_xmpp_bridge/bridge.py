"""Main bridge daemon — orchestrator composing all components."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import json
import logging
import os
import signal
import time
import uuid
from pathlib import Path

import slixmpp

from . import __version__
from .audit import AuditLogger
from .config import DEFAULT_SOURCE_ICONS, MAX_SOURCE_LEN, Config
from .email_notify import send_email
from .mcp_server import BridgeMCPServer
from .messages import Messages, format_generated_agent_message, load_messages
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

# Maximum age of the last "state" heartbeat before a session is considered dead.
# The plugin sends a heartbeat every ~90 s (REREG_INTERVAL_MS).  Three missed
# heartbeats = 270 s, so 300 s (5 min) gives a comfortable margin.
HEARTBEAT_TTL = 300.0  # seconds


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
        self._ask_queue: collections.deque[_PendingAsk] = collections.deque()
        self.mcp_server: BridgeMCPServer | None = BridgeMCPServer(config.mcp_port) if config.mcp_port else None
        # Per-STY lock to serialize screen -Q queries (concurrent queries on the
        # same session create colliding -queryA sockets and return exit 1).
        self._screen_query_locks: dict[str, asyncio.Lock] = {}
        # Merged source icons (defaults + user config), computed once.
        self._icons: dict[str | None, str] = {**DEFAULT_SOURCE_ICONS, **config.source_icons}

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

    @staticmethod
    def _sorted_ids(sessions: dict[str, SessionInfo]) -> list[str]:
        """Return session IDs sorted by registration time (oldest first)."""
        return sorted(sessions, key=lambda s: sessions[s]["registered_at"])

    @staticmethod
    def _plugin_display_ref(plugin_version: str | None) -> str:
        """Format plugin build info for compact human display."""
        if not plugin_version:
            return ""
        version = str(plugin_version).strip()
        if not version:
            return ""
        if "+" in version:
            _base, build = version.split("+", 1)
            return f" @{build}" if build else ""
        return f" v{version}"

    @staticmethod
    def _optional_int(value: object) -> int | None:
        """Convert a socket/MCP request field to int if present."""
        return int(str(value)) if value is not None else None

    async def _cmd_list(self) -> None:
        await self._cleanup_stale_sessions()
        sessions = self.registry.list_sessions()
        if not sessions:
            self._xmpp_send(self.messages.no_sessions)
            return

        lines = [self.messages.session_list_header]
        sorted_ids = self._sorted_ids(sessions)
        active_id = self.registry.last_active
        for i, sid in enumerate(sorted_ids, 1):
            info = sessions[sid]
            marker = " *" if sid == active_id else ""
            backend = info["backend"]
            source = info.get("source")
            source_icon = self._source_icon(source)
            window_label = self._window_label(info)
            state = info.get("agent_state") or ""
            mode = info.get("agent_mode") or ""
            version = info.get("plugin_version") or ""
            project = info["project"]

            # Agent/mode icon — plugin sends emoji directly (e.g. 🟠 for coder).
            # Use as-is; empty string if not set.
            mode_icon = mode

            # State icon — matches the circles used in the Screen window title
            if state == "idle":
                state_icon = "🟢"
            elif state == "running":
                state_icon = "🔵"
            else:
                state_icon = state  # raw value fallback (e.g. "interaction" → 🔴 sent by plugin)

            # Icons prefix: source + mode + state
            icons = source_icon + mode_icon + (state_icon if state else "")

            # Backend bracket (no icons inside)
            if backend == "screen":
                tag = f"[screen{window_label}]"
            elif backend == "tmux":
                tag = f"[tmux{window_label}]"
            else:
                tag = f"[{self.messages.read_only_tag}]"

            meta = self._plugin_display_ref(version)
            lines.append(f"  /{i}  {icons}  {tag}{meta}  {self._short_path(project)}{marker}")
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

    # Agent states that block direct screen injection.
    # When the agent is in one of these states, screen inject would interfere
    # with the permission prompt — text gets pasted into the ask dialog and
    # the trailing CR confirms/rejects it with garbage input.
    _ASKING_STATES: frozenset[str] = frozenset({"asking", "waiting_for_permission"})

    async def _stuff_to_session(
        self,
        session_id: str,
        info: SessionInfo,
        text: str,
        *,
        asking_guard: bool = False,
        from_session: str | None = None,
        source_type: str | None = None,
        message_type: str | None = None,
    ) -> bool:
        """Send *text* to a terminal session via the multiplexer.

        When *asking_guard* is True and the target agent's ``agent_state`` is
        one of the asking states (``asking``, ``waiting_for_permission``), the
        method **does not** inject text into the terminal.  Instead it falls
        back to inbox enqueue + CR nudge so the message is safely delivered
        without interfering with the permission prompt.

        The extra keyword arguments (*from_session*, *source_type*,
        *message_type*) are only used when the asking-guard fallback fires —
        they are forwarded to :meth:`_enqueue_for_mcp`.
        """
        mux = get_multiplexer(info["backend"])
        if not mux:
            log.warning("No backend for session (project=%s)", info["project"])
            return False

        agent_state = info.get("agent_state") or ""
        if asking_guard and agent_state in self._ASKING_STATES:
            log.info(
                "Asking guard: agent %s is in state %r — falling back to inbox+nudge",
                session_id,
                agent_state,
            )
            self._enqueue_for_mcp(
                session_id,
                text,
                from_session=from_session,
                source_type=source_type,
                message_type=message_type,
            )
            nudge_ok = await mux.send_nudge(info["sty"], info["window"])
            self.audit.log(
                "TERMINAL_SEND_ASKING_FALLBACK",
                session_id=session_id,
                project=info["project"],
                backend=info["backend"],
                agent_state=agent_state,
                nudge_ok=nudge_ok,
                text_len=len(text),
            )
            return True  # message is safely queued

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

    async def _nudge_session(
        self,
        session_id: str,
        info: SessionInfo,
        message: str,
        *,
        from_session: str | None = None,
        source_type: str | None = None,
        message_type: str | None = None,
    ) -> bool:
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
        self._enqueue_for_mcp(
            session_id,
            message,
            from_session=from_session,
            source_type=source_type,
            message_type=message_type,
        )
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
        """Send *text* to the recipient via XMPP.

        If SMTP relay is configured and *text* exceeds *email_threshold*
        characters, the full message is also delivered by email.  The XMPP
        notification is replaced by a truncated snippet with a note that the
        full content was sent by email.
        """
        cfg = self.config
        email_triggered = False
        if cfg.smtp_host and len(text) > cfg.email_threshold:
            email_triggered = True
            snippet = text[: cfg.email_threshold]
            # Ensure we don't cut in the middle of a multi-byte sequence
            xmpp_body = f"{snippet}\n\n[… {len(text)} chars total — full message sent by email]"
            log.info(
                "Email relay triggered: %d chars (threshold %d), sending via %s:%d",
                len(text),
                cfg.email_threshold,
                cfg.smtp_host,
                cfg.smtp_port,
            )
            # Fire-and-forget email delivery (non-blocking)
            task = asyncio.create_task(
                send_email(
                    smtp_host=cfg.smtp_host,
                    smtp_port=cfg.smtp_port,
                    sender=cfg.recipient,
                    recipient=cfg.recipient,
                    subject=f"[bridge] {text[:80].splitlines()[0]}",
                    body=text,
                )
            )
            task.add_done_callback(self._email_task_done)
        else:
            xmpp_body = text
        ok = self.xmpp.send(cfg.recipient, xmpp_body)
        self.audit.log(
            "XMPP_OUT",
            recipient=cfg.recipient,
            body_len=len(xmpp_body),
            original_len=len(text),
            email_relay=email_triggered,
            ok=ok,
        )
        return ok

    @staticmethod
    def _email_task_done(task: asyncio.Task[bool]) -> None:
        """Callback for fire-and-forget email tasks — log unexpected errors."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Email task raised unexpected error: %s", exc)

    def _enqueue_for_mcp(
        self,
        session_id: str,
        message: str,
        *,
        from_session: str | None = None,
        source_type: str | None = None,
        message_type: str | None = None,
    ) -> None:
        """Queue *message* into the MCP inbox for *session_id* (no-op if MCP disabled)."""
        if self.mcp_server is not None:
            self.mcp_server.enqueue(
                session_id,
                message,
                from_session=from_session,
                source_type=source_type,
                message_type=message_type,
            )

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
        icons = self._icons
        return icons.get(source) or icons.get(None, "⚡")

    def _session_prefix(self, info: SessionInfo) -> str:
        """Return icon + bracketed project + window label, e.g. '⚡[~/foo #2]'."""
        icon = self._source_icon(info.get("source"))
        project = self._short_path(info["project"])
        loc = self._window_label(info)
        return f"{icon}[{project}{loc}]"

    # --- Stale session cleanup ---

    def _screen_socket_alive(self, sty: str) -> bool:
        """Return True if the GNU Screen socket file for *sty* exists.

        This is the fast (no subprocess) session-level liveness check.  The
        socket file is owned by the user and accessible without a TTY, unlike
        ``screen -Q title`` which fails when called from a process without a
        controlling terminal (e.g. a systemd user service).
        """
        sock = self._screen_socket_path(sty)
        if sock is None:
            return True  # cannot determine path — assume alive
        return sock.exists()

    async def _screen_window_alive(self, sty: str, window: str) -> bool:
        """Return True if screen window *window* exists within session *sty*.

        Uses ``screen -S <sty> -p <window> -Q title`` which works reliably from
        a TTY-less process (verified: exit 0 if window exists, exit 1 with
        "Could not find pre-select window" if not).  Returns True on timeout or
        subprocess error to avoid false positives.

        Calls are serialized per *sty* because concurrent ``-Q`` queries on the
        same screen session create colliding ``-queryA`` sockets and the later
        ones return exit 1 with "There is already a screen running".
        """
        lock = self._screen_query_locks.get(sty)
        if lock is None:
            lock = asyncio.Lock()
            self._screen_query_locks[sty] = lock

        async with lock:
            env: dict[str, str] = {}
            for var in ("PATH", "USER", "HOME", "SCREENDIR"):
                if var in os.environ:
                    env[var] = os.environ[var]
            win = window or "0"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "screen",
                    "-S",
                    sty,
                    "-p",
                    win,
                    "-Q",
                    "title",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                )
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                return proc.returncode == 0
            except (TimeoutError, OSError) as exc:
                log.debug("_screen_window_alive: assume alive on error (sty=%s, win=%s, exc=%s)", sty, win, exc)
                return True  # assume alive on error

    def _screen_socket_path(self, sty: str) -> Path | None:
        """Return the path to the GNU Screen socket file for *sty*, or None if
        the SCREENDIR cannot be determined."""
        # Screen looks for sockets in $SCREENDIR, then $HOME/.screen, then
        # /run/screen/S-$USER (distro default).  We check in the same order.
        screendir = os.environ.get("SCREENDIR")
        if screendir:
            return Path(screendir) / sty
        home = os.environ.get("HOME")
        if home:
            candidate = Path(home) / ".screen" / sty
            if candidate.parent.exists():
                return candidate
        user = os.environ.get("USER")
        if user:
            return Path(f"/run/screen/S-{user}") / sty
        return None

    async def _is_session_alive(self, info: SessionInfo) -> bool:
        """Check if the session's terminal multiplexer window is still running.

        For screen (three-stage check):
          1. Socket file existence (fast, no subprocess) — if missing, session dead.
          2. Window-level subprocess check (``screen -S <sty> -p <win> -Q title``) —
             if the specific window no longer exists, session dead.
          3. Heartbeat TTL — if the plugin's last ``state`` call is older than
             HEARTBEAT_TTL seconds, session is considered dead.  The plugin sends
             a heartbeat every ~90 s via its re-register timer; three missed
             heartbeats (270 s) is well within the 300 s TTL.

        For tmux: ``tmux has-session -t <sty>`` (session-level).
        """
        backend = info["backend"]
        if backend is None:
            return True  # read-only sessions can't be verified
        sty = info["sty"]
        if not sty:
            return True  # no sty — can't check

        if backend == "screen":
            slot = f"{sty}:{info.get('window') or '0'}"

            # Stage 1: socket file (fast path, no subprocess)
            if not self._screen_socket_alive(sty):
                log.debug("_is_session_alive: dead — socket missing (slot=%s)", slot)
                return False

            # Stage 2: window-level check
            window = info.get("window") or "0"
            if not await self._screen_window_alive(sty, window):
                log.debug("_is_session_alive: dead — window missing (slot=%s)", slot)
                return False

            # Stage 3: heartbeat TTL
            last_seen = info.get("last_seen")
            if last_seen is not None:
                age = time.time() - last_seen
                if age > HEARTBEAT_TTL:
                    log.debug(
                        "_is_session_alive: dead — heartbeat stale (slot=%s, age=%.0fs)",
                        slot,
                        age,
                    )
                    return False

            return True

        elif backend == "tmux":
            slot = sty
            env = {}
            for var in ("PATH", "USER", "HOME"):
                if var in os.environ:
                    env[var] = os.environ[var]
            cmd = ["tmux", "has-session", "-t", sty]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                log.debug("_is_session_alive: timeout (slot=%s cmd=%s)", slot, cmd)
                return False
            except OSError as exc:
                log.debug("_is_session_alive: OSError (slot=%s cmd=%s err=%s)", slot, cmd, exc)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return False
            alive = proc.returncode == 0
            if not alive:
                log.debug("_is_session_alive: dead (slot=%s cmd=%s rc=%s)", slot, cmd, proc.returncode)
            return alive

        else:
            return True  # unknown backend — assume alive

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
        legacy_removed = self._cleanup_legacy_lock_hints()
        if to_remove:
            log.info(self.messages.stale_sessions_cleaned.format(count=len(to_remove)))
            # Prune _screen_query_locks — remove locks for STY values that no
            # longer have any registered session.  Without this, the dict grows
            # unbounded over the lifetime of the bridge process.
            active_stys = {info["sty"] for info in self.registry.sessions.values() if info.get("sty")}
            stale_stys = [sty for sty in self._screen_query_locks if sty not in active_stys]
            for sty in stale_stys:
                del self._screen_query_locks[sty]
            if stale_stys:
                log.debug("Pruned %d stale screen query lock(s)", len(stale_stys))
            # Prune MCP client→session mappings that point to unregistered sessions.
            if self.mcp_server is not None:
                active_sids = set(self.registry.sessions.keys())
                self.mcp_server.prune_stale_client_sessions(active_sids)
        if legacy_removed:
            log.info("Cleaned %d stale legacy lock hint(s)", legacy_removed)
        return len(to_remove)

    def _cleanup_legacy_lock_hints(self) -> int:
        """Remove stale lock hint files from ``~/.claude/working``."""
        lock_dir = Path.home() / ".claude" / "working"
        if not lock_dir.is_dir():
            return 0

        active_sessions = set(self.registry.sessions)
        removed = 0
        for path in lock_dir.iterdir():
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            session_id = str(data.get("session_id", "")).strip()
            if session_id and session_id not in active_sessions:
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
        return removed

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
            if self._ask_queue and self._ask_queue[0] is pending:
                self._ask_queue.popleft()
            elif pending in self._ask_queue:
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
        elif cmd == "get_context":
            response = self._handle_get_context(req)
        elif cmd == "list_todos":
            response = self._handle_list_todos(req)
        elif cmd == "replace_todos":
            response = self._handle_replace_todos(req)
        elif cmd == "add_todo":
            response = self._handle_add_todo(req)
        elif cmd == "update_todo":
            response = self._handle_update_todo(req)
        elif cmd == "remove_todo":
            response = self._handle_remove_todo(req)
        elif cmd == "reply_to_last_sender":
            response = await self._handle_reply_to_last_sender(req)
        elif cmd == "list_file_locks":
            response = self._handle_list_file_locks(req)
        elif cmd == "acquire_file_lock":
            response = self._handle_acquire_file_lock(req)
        elif cmd == "release_file_lock":
            response = self._handle_release_file_lock(req)
        elif cmd == "cleanup_stale_locks":
            response = self._handle_cleanup_stale_locks(req)
        elif cmd == "delegate":
            response = await self._handle_delegate(req)
        elif cmd == "task_result":
            response = await self._handle_task_result(req)
        elif cmd == "list_tasks":
            response = self._handle_list_tasks(req)
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
                same_screen_slot = backend == "screen" and old_info["sty"] == sty and old_info["window"] == window
                same_tmux_slot = backend == "tmux" and old_info["sty"] == sty and old_info["window"] == window
                if sty and (same_screen_slot or same_tmux_slot):
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
            if self.mcp_server is not None:
                self.mcp_server.note_session_registration(sid, source=source)
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
        """Update the agent_state (and optionally agent_mode) for a registered session.

        Protocol fields:
          - ``session_id`` : session to update (required)
          - ``state``      : new state string, e.g. "idle" or "running" (required)
          - ``mode``       : new mode string, e.g. "planning", "code", "build" (optional)
        """
        sid = str(req.get("session_id", ""))
        if not sid:
            return {"error": "missing session_id"}
        state = str(req.get("state", ""))
        if not state:
            return {"error": "missing state"}
        mode_raw = req.get("mode")
        mode = str(mode_raw) if mode_raw is not None else None
        updated = self.registry.update_state(sid, state, mode=mode)
        if not updated:
            return {"error": f"session not found: {sid}"}
        self.audit.log("SESSION_STATE", session_id=sid, state=state, mode=mode)
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
        sorted_ids = self._sorted_ids(sessions)
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

        if not nudge and not target_info["backend"]:
            return {
                "ok": False,
                "error": self.messages.relay_no_backend.format(project=self._short_path(target_info["project"])),
            }

        # Build XMPP observer label
        sender_id = str(req.get("session_id", ""))
        message_id = uuid.uuid4().hex[:12]
        # Inter-agent relay always uses inbox+nudge to avoid race conditions
        # with permission prompts.  The ``nudge`` parameter is now ignored
        # (always True) but kept for API compatibility.
        wrapped_message = format_generated_agent_message(
            msg_type="relay",
            message=message,
            from_session_id=sender_id or None,
            to_session_id=target_id,
            mode="nudge",
            message_id=message_id,
        )

        if target_info["backend"]:
            ok = await self._nudge_session(
                target_id,
                target_info,
                wrapped_message,
                from_session=sender_id or None,
                source_type="agent",
                message_type="relay",
            )
        else:
            self._enqueue_for_mcp(
                target_id,
                wrapped_message,
                from_session=sender_id or None,
                source_type="agent",
                message_type="relay",
            )
            ok = True

        self.audit.log(
            "RELAY_SENT" if ok else "RELAY_FAILED",
            from_session_id=sender_id or None,
            to_session_id=target_id,
            message=message,
            message_len=len(message),
        )

        mode = "nudge"
        if ok:
            # Notify observer so they can see inter-agent traffic (JSON format)
            xmpp_payload = json.dumps(
                {
                    "type": "relay",
                    "mode": mode,
                    "from": sender_id or None,
                    "to": target_id,
                    "message_id": message_id,
                    "message": message,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
            self._xmpp_send(xmpp_payload)
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
        message_id = uuid.uuid4().hex[:12]

        targets = {
            sid: info
            for sid, info in self.registry.sessions.items()
            if sid != sender_id and (nudge or info.get("backend"))
        }

        if not targets:
            return {"ok": True, "delivered": 0}

        # Inter-agent broadcast always uses inbox+nudge to avoid race
        # conditions with permission prompts.
        wrapped_message = format_generated_agent_message(
            msg_type="broadcast",
            message=message,
            from_session_id=sender_id or None,
            mode="nudge",
            message_id=message_id,
        )

        async def _deliver_nudge(sid: str, info: SessionInfo) -> bool:
            if info.get("backend"):
                return await self._nudge_session(
                    sid,
                    info,
                    wrapped_message,
                    from_session=sender_id or None,
                    source_type="agent",
                    message_type="broadcast",
                )
            self._enqueue_for_mcp(
                sid,
                wrapped_message,
                from_session=sender_id or None,
                source_type="agent",
                message_type="broadcast",
            )
            return True

        results = await asyncio.gather(*(_deliver_nudge(sid, info) for sid, info in targets.items()))

        delivered: list[str] = []
        failed: list[str] = []
        for (_sid, info), ok in zip(targets.items(), results, strict=True):
            if ok:
                delivered.append(self._session_prefix(info))
            else:
                failed.append(self._session_prefix(info))

        self.audit.log(
            "BROADCAST_SENT",
            from_session_id=sender_id or None,
            delivered=len(delivered),
            failed=len(failed),
            message=message,
            message_len=len(message),
        )

        # Single XMPP summary for the observer (JSON format)
        mode = "nudge" if nudge else "screen"
        delivered_sids = [sid for (sid, _info), ok in zip(targets.items(), results, strict=True) if ok]
        xmpp_payload = json.dumps(
            {
                "type": "broadcast",
                "mode": mode,
                "from": sender_id or None,
                "to": delivered_sids,
                "message_id": message_id,
                "message": message,
                "ts": time.time(),
            },
            ensure_ascii=False,
        )
        self._xmpp_send(xmpp_payload)

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
        sorted_ids = self._sorted_ids(sessions)
        result = []
        for i, sid in enumerate(sorted_ids, 1):
            info = sessions[sid]
            result.append(self._session_entry(sid, info, index=i, include_registered_at=True))
        return {"ok": True, "sessions": result}

    def _legacy_project_matches(self, lock_project: str, lock_filepath: str, project: str) -> bool:
        if not project:
            return True
        short = self._short_path(project)
        return lock_project in {project, short} or lock_filepath.startswith(project)

    def _session_counts(self, session_id: str) -> dict[str, int]:
        return {
            "inbox_count": self.registry.inbox_count(session_id),
            "todo_count": self.registry.todo_count(session_id),
            "lock_count": self.registry.file_lock_count(session_id),
        }

    @staticmethod
    def _idle_seconds(info: SessionInfo) -> int | None:
        last_seen = info.get("last_seen")
        if last_seen is None:
            return None
        if info.get("agent_state") != "idle":
            return 0
        return max(0, int(time.time() - last_seen))

    def _session_entry(
        self,
        session_id: str,
        info: SessionInfo,
        *,
        index: int | None = None,
        include_registered_at: bool = False,
        normalize_empty: bool = False,
    ) -> dict[str, object]:
        entry: dict[str, object] = {
            "session_id": session_id,
            "project": info["project"],
            "backend": (info.get("backend") or "null") if normalize_empty else info.get("backend"),
            "source": (info.get("source") or "") if normalize_empty else info.get("source"),
            "window": info.get("window") or "",
            "sty": info.get("sty") or "",
            "plugin_version": (info.get("plugin_version") or "") if normalize_empty else info.get("plugin_version"),
            "agent_state": (info.get("agent_state") or "") if normalize_empty else info.get("agent_state"),
            "agent_mode": (info.get("agent_mode") or "") if normalize_empty else info.get("agent_mode"),
            "last_seen": info.get("last_seen"),
            "idle_seconds": self._idle_seconds(info),
            "todos_version": info.get("todos_version", 0),
            "last_agent_sender": (
                (info.get("last_agent_sender") or "") if normalize_empty else info.get("last_agent_sender")
            ),
            **self._session_counts(session_id),
        }
        if index is not None:
            entry["index"] = index
        if include_registered_at:
            entry["registered_at"] = info["registered_at"]
        return entry

    def _session_context_payload(
        self, session_id: str, info: SessionInfo, *, normalize_empty: bool
    ) -> dict[str, object]:
        return {
            "ok": True,
            "session": self._session_entry(session_id, info, normalize_empty=normalize_empty),
            "todos": [dict(todo) for todo in self.registry.list_todos(session_id)],
            "file_locks": [dict(lock) for lock in self.registry.list_file_locks_for_session(session_id)],
        }

    def _list_file_lock_payloads(self, *, project: str = "", include_stale: bool = True) -> list[dict[str, object]]:
        locks = [
            {**dict(lock), "stale": lock["session_id"] not in self.registry.sessions, "source": "bridge"}
            for lock in self.registry.list_file_locks()
            if self._legacy_project_matches(lock["project"], lock["filepath"], project)
        ]
        locks.extend(self._read_legacy_lock_hints(project=project))
        if not include_stale:
            locks = [lock for lock in locks if not lock["stale"]]
        for lock in locks:
            lock.pop("lockfile", None)
        locks.sort(key=lambda item: (str(item.get("locked_at", "")), str(item.get("filepath", ""))))
        return locks

    def _cleanup_stale_lock_payloads(self, *, project: str = "") -> list[dict[str, object]]:
        removed: list[dict[str, object]] = []
        for lock in self.registry.list_file_locks():
            if lock["session_id"] in self.registry.sessions:
                continue
            if not self._legacy_project_matches(lock["project"], lock["filepath"], project):
                continue
            self.registry.release_file_lock(lock["session_id"], lock["filepath"], force=True)
            removed.append({**dict(lock), "stale": True, "source": "bridge"})
        for legacy_lock in self._read_legacy_lock_hints(project=project):
            if not legacy_lock["stale"]:
                continue
            lockfile = legacy_lock.get("lockfile")
            if isinstance(lockfile, str):
                with contextlib.suppress(OSError):
                    Path(lockfile).unlink()
            result = dict(legacy_lock)
            result.pop("lockfile", None)
            removed.append(result)
        return removed

    def _read_legacy_lock_hints(self, project: str = "") -> list[dict[str, object]]:
        lock_dir = Path.home() / ".claude" / "working"
        if not lock_dir.is_dir():
            return []
        active_sessions = set(self.registry.sessions)
        locks: list[dict[str, object]] = []
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
            if not self._legacy_project_matches(lock_project, filepath, project):
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
        return locks

    def _handle_get_context(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        info = self.registry.get(session_id)
        if not info:
            return {"error": f"unknown session_id: {session_id}"}
        return self._session_context_payload(session_id, info, normalize_empty=True)

    def _handle_list_todos(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        return {"ok": True, "todos": [dict(todo) for todo in self.registry.list_todos(session_id)]}

    def _handle_replace_todos(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        info = self.registry.get(session_id)
        if not info:
            return {"error": f"unknown session_id: {session_id}"}
        todos = req.get("todos")
        if not isinstance(todos, list):
            return {"error": "todos must be a list"}
        expected_version_raw = req.get("expected_version")
        expected_version = self._optional_int(expected_version_raw)
        version = self.registry.replace_todos(session_id, todos, expected_version=expected_version)
        if version is None:
            return {"error": "todo version conflict", "current_version": info.get("todos_version", 0)}
        return {"ok": True, "count": len(todos), "version": version}

    def _handle_add_todo(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        content = str(req.get("content", "")).strip()
        if not content:
            return {"error": "missing content"}
        expected_version_raw = req.get("expected_version")
        expected_version = self._optional_int(expected_version_raw)
        todo, version = self.registry.add_todo(
            session_id,
            content,
            status=str(req.get("status", "pending")),
            priority=str(req.get("priority", "medium")),
            expected_version=expected_version,
        )
        info = self.registry.get(session_id)
        if todo is None or version is None:
            current_version = info.get("todos_version", 0) if info else 0
            if info:
                return {"error": "todo version conflict", "current_version": current_version}
            return {"error": f"unknown session_id: {session_id}"}
        return {"ok": True, "todo": dict(todo), "version": version}

    def _handle_update_todo(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        todo_id = str(req.get("todo_id", ""))
        if not todo_id:
            return {"error": "missing todo_id"}
        info = self.registry.get(session_id)
        if not info:
            return {"error": f"unknown session_id: {session_id}"}
        expected_version_raw = req.get("expected_version")
        expected_version = self._optional_int(expected_version_raw)
        todo, version = self.registry.update_todo(
            session_id,
            todo_id,
            content=str(req["content"]) if "content" in req and req["content"] is not None else None,
            status=str(req["status"]) if "status" in req and req["status"] is not None else None,
            priority=str(req["priority"]) if "priority" in req and req["priority"] is not None else None,
            expected_version=expected_version,
        )
        if todo is None or version is None:
            if info and expected_version is not None and expected_version != info.get("todos_version", 0):
                return {"error": "todo version conflict", "current_version": info.get("todos_version", 0)}
            return {"error": f"todo not found: {todo_id}"}
        return {"ok": True, "todo": dict(todo), "version": version}

    def _handle_remove_todo(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        todo_id = str(req.get("todo_id", ""))
        if not todo_id:
            return {"error": "missing todo_id"}
        info = self.registry.get(session_id)
        if not info:
            return {"error": f"unknown session_id: {session_id}"}
        expected_version_raw = req.get("expected_version")
        expected_version = self._optional_int(expected_version_raw)
        removed, version = self.registry.remove_todo(session_id, todo_id, expected_version=expected_version)
        if version is None:
            if info and expected_version is not None and expected_version != info.get("todos_version", 0):
                return {"error": "todo version conflict", "current_version": info.get("todos_version", 0)}
            return {"error": f"todo not found: {todo_id}"}
        return {"ok": True, "removed": removed, "version": version}

    async def _handle_reply_to_last_sender(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", "")).strip()
        message = str(req.get("message", "")).strip()
        if not session_id:
            return {"error": "missing session_id"}
        if not message:
            return {"error": "missing message"}
        info = self.registry.get(session_id)
        if not info:
            return {"error": f"unknown session_id: {session_id}"}
        last_sender = self.registry.get_last_agent_sender(session_id)
        if not last_sender:
            return {"error": f"no known sender to reply to for {session_id}"}
        target_info = self.registry.get(last_sender)
        if not target_info:
            return {"error": f"reply target not found: {last_sender}"}
        # Inter-agent reply always uses inbox+nudge to avoid race conditions
        # with permission prompts.
        message_id = uuid.uuid4().hex[:12]
        wrapped_message = format_generated_agent_message(
            msg_type="relay",
            message=message,
            from_session_id=session_id,
            to_session_id=last_sender,
            mode="nudge",
            message_id=message_id,
        )
        if target_info.get("backend"):
            ok = await self._nudge_session(
                last_sender,
                target_info,
                wrapped_message,
                from_session=session_id,
                source_type="agent",
                message_type="relay",
            )
        else:
            self._enqueue_for_mcp(
                last_sender,
                wrapped_message,
                from_session=session_id,
                source_type="agent",
                message_type="relay",
            )
            ok = True
        mode = "nudge"
        if ok:
            self._xmpp_send(
                json.dumps(
                    {
                        "type": "relay",
                        "mode": mode,
                        "from": session_id,
                        "to": last_sender,
                        "message_id": message_id,
                        "message": message,
                        "ts": time.time(),
                    },
                    ensure_ascii=False,
                )
            )
            self.audit.log(
                "RELAY_SENT",
                from_session_id=session_id,
                to_session_id=last_sender,
                message=message,
                message_len=len(message),
                via="reply_to_last_sender",
            )
            return {"ok": True, "to": last_sender, "mode": mode}
        self.audit.log(
            "RELAY_FAILED",
            from_session_id=session_id,
            to_session_id=last_sender,
            message=message,
            message_len=len(message),
            via="reply_to_last_sender",
        )
        return {"error": self.messages.relay_failed.format(project=self._short_path(target_info["project"]))}

    def _handle_list_file_locks(self, req: dict[str, object]) -> dict[str, object]:
        project = str(req.get("project", ""))
        include_stale = bool(req.get("include_stale", True))
        return {"ok": True, "locks": self._list_file_lock_payloads(project=project, include_stale=include_stale)}

    def _handle_acquire_file_lock(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        filepath = str(req.get("filepath", "")).strip()
        if not filepath:
            return {"error": "missing filepath"}
        info = self.registry.get(session_id)
        if not info:
            return {"error": f"unknown session_id: {session_id}"}
        acquired, lock, replaced_stale = self.registry.acquire_file_lock(
            session_id,
            filepath,
            str(req.get("project", "")).strip() or info["project"],
            str(req.get("reason", "")).strip() or None,
        )
        return {"ok": acquired, "lock": dict(lock), "replaced_stale": replaced_stale}

    def _handle_release_file_lock(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        filepath = str(req.get("filepath", "")).strip()
        if not filepath:
            return {"error": "missing filepath"}
        released = self.registry.release_file_lock(session_id, filepath, force=bool(req.get("force", False)))
        return {"ok": True, "released": released}

    def _handle_cleanup_stale_locks(self, req: dict[str, object]) -> dict[str, object]:
        project = str(req.get("project", ""))
        removed = self._cleanup_stale_lock_payloads(project=project)
        return {"ok": True, "removed": len(removed), "locks": removed}

    # --- Task delegation ---

    async def _handle_delegate(self, req: dict[str, object]) -> dict[str, object]:
        """Delegate a task to another agent via the bridge.

        Protocol fields:
          - ``to``           : target session_id (required)
          - ``description``  : what the target should do (required)
          - ``context``      : optional additional context
          - ``session_id``   : sender (delegator) session_id
          - ``nudge``        : if True, nudge target (default: True)
        """
        description = str(req.get("description", ""))
        if not description:
            return {"ok": False, "error": "missing description"}

        to_raw = req.get("to")
        if not to_raw:
            return {"ok": False, "error": "missing target session_id (to)"}
        target_id = str(to_raw)
        target_info = self.registry.get(target_id)
        if not target_info:
            return {"ok": False, "error": f"unknown target session: {target_id}"}

        sender_id = str(req.get("session_id", ""))
        context = str(req.get("context", "")) or None
        nudge = bool(req.get("nudge", True))

        task_id = uuid.uuid4().hex[:12]
        task = self.registry.task_create(
            task_id=task_id,
            from_session=sender_id or "unknown",
            to_session=target_id,
            description=description,
            context=context,
        )

        # Build task_request message
        task_msg = json.dumps(
            {
                "type": "task_request",
                "task_id": task_id,
                "from": sender_id or None,
                "description": description,
                "context": context,
            },
            ensure_ascii=False,
        )
        wrapped = format_generated_agent_message(
            msg_type="task_request",
            message=task_msg,
            from_session_id=sender_id or None,
            to_session_id=target_id,
            mode="nudge" if nudge else "inbox",
            message_id=task_id,
        )

        ok = True
        if nudge and target_info.get("backend"):
            ok = await self._nudge_session(
                target_id,
                target_info,
                wrapped,
                from_session=sender_id or None,
                source_type="agent",
                message_type="task_request",
            )
        else:
            self._enqueue_for_mcp(
                target_id,
                wrapped,
                from_session=sender_id or None,
                source_type="agent",
                message_type="task_request",
            )

        self._xmpp_send(
            json.dumps(
                {
                    "type": "task_request",
                    "task_id": task_id,
                    "from": sender_id or None,
                    "to": target_id,
                    "description": description,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
        )

        self.audit.log(
            "TASK_DELEGATE",
            task_id=task_id,
            from_session_id=sender_id or None,
            to_session_id=target_id,
            description=description[:100],
            ok=ok,
        )

        return {"ok": ok, "task_id": task_id, "task": dict(task)}

    async def _handle_task_result(self, req: dict[str, object]) -> dict[str, object]:
        """Report the result of a delegated task.

        Protocol fields:
          - ``task_id``      : task ID (required)
          - ``status``       : new status — accepted/completed/failed/cancelled (required)
          - ``result``       : result text (optional)
          - ``session_id``   : sender (assignee) session_id
          - ``nudge``        : if True, nudge the delegator (default: True)
        """
        task_id = str(req.get("task_id", ""))
        if not task_id:
            return {"ok": False, "error": "missing task_id"}
        status = str(req.get("status", ""))
        valid_statuses = {"accepted", "completed", "failed", "cancelled"}
        if status not in valid_statuses:
            return {"ok": False, "error": f"invalid status: {status} (must be one of {sorted(valid_statuses)})"}

        sender_id = str(req.get("session_id", ""))
        result = str(req.get("result", "")) or None
        nudge = bool(req.get("nudge", True))

        try:
            task = self.registry.task_update_status(task_id, status, result)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if task is None:
            return {"ok": False, "error": f"task not found: {task_id}"}

        # Deliver result to the delegator
        delegator = task["from_session"]
        delegator_info = self.registry.get(delegator)

        result_msg = json.dumps(
            {
                "type": "task_result",
                "task_id": task_id,
                "from": sender_id or task["to_session"],
                "status": status,
                "result": result,
                "description": task["description"],
            },
            ensure_ascii=False,
        )
        wrapped = format_generated_agent_message(
            msg_type="task_result",
            message=result_msg,
            from_session_id=sender_id or task["to_session"],
            to_session_id=delegator,
            mode="nudge" if nudge else "inbox",
            message_id=task_id,
        )

        ok = True
        if delegator_info is not None:
            if nudge and delegator_info.get("backend"):
                ok = await self._nudge_session(
                    delegator,
                    delegator_info,
                    wrapped,
                    from_session=sender_id or task["to_session"],
                    source_type="agent",
                    message_type="task_result",
                )
            else:
                self._enqueue_for_mcp(
                    delegator,
                    wrapped,
                    from_session=sender_id or task["to_session"],
                    source_type="agent",
                    message_type="task_result",
                )

        self._xmpp_send(
            json.dumps(
                {
                    "type": "task_result",
                    "task_id": task_id,
                    "from": sender_id or task["to_session"],
                    "to": delegator,
                    "status": status,
                    "result": (result or "")[:200],
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
        )

        self.audit.log(
            "TASK_RESULT",
            task_id=task_id,
            from_session_id=sender_id or task["to_session"],
            to_session_id=delegator,
            status=status,
            ok=ok,
        )

        return {"ok": ok, "task": dict(task)}

    def _handle_list_tasks(self, req: dict[str, object]) -> dict[str, object]:
        """List delegated tasks, optionally filtered.

        Protocol fields:
          - ``session_id``  : filter by session (optional)
          - ``role``        : ``"from"``, ``"to"``, or ``"both"`` (default)
          - ``status``      : filter by status (optional)
        """
        session_id = str(req.get("session_id", "")) or None
        role = str(req.get("role", "both"))
        status = str(req.get("status", "")) or None
        tasks = self.registry.task_list(session_id=session_id, role=role, status=status)
        return {"ok": True, "tasks": [dict(t) for t in tasks]}

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
        self._xmpp_send(f"{self.messages.bridge_stopped} (v{__version__})")
        await asyncio.sleep(1)
        self.xmpp.disconnect()
        self.registry.close()
        self.audit.close()
        log.info("Bye.")
