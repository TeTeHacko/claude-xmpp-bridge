"""Main bridge daemon — orchestrator composing all components."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import slixmpp

from .config import Config
from .messages import Messages, load_messages
from .multiplexer import get_multiplexer
from .registry import SessionInfo, SessionRegistry
from .socket_server import SocketServer
from .xmpp import XMPPConnection

log = logging.getLogger(__name__)

_ALIVE_CHECK_CMDS: dict[str, list[str]] = {
    "screen": ["screen", "-ls"],
    "tmux": ["tmux", "has-session", "-t"],
}


class XMPPBridge:
    """Orchestrator that composes XMPP, socket server, registry, and multiplexer."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.messages: Messages = load_messages(config.messages_file)
        self.registry = SessionRegistry(config.db_path)
        self.xmpp = XMPPConnection(config.jid, config.password)
        self.xmpp.on_message(self._on_xmpp_message)
        self.socket_server = SocketServer(config.socket_path, self._handle_request)

    # --- XMPP message handling ---

    async def _on_xmpp_message(self, msg: slixmpp.Message) -> None:
        if msg["type"] not in ("chat", "normal"):
            return
        if msg["from"].bare != self.config.recipient:
            return

        body: str = msg["body"].strip()
        if not body:
            return

        log.info("XMPP message: %s", body[:100])

        if body.startswith("/"):
            await self._handle_command(body)
        else:
            await self._send_to_session(None, body)

    async def _handle_command(self, body: str) -> None:
        parts = body.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/list", "/l"):
            await self._cmd_list()
        elif cmd == "/help":
            await self._cmd_help()
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
            if backend == "screen":
                tag = "[screen]"
            elif backend == "tmux":
                tag = "[tmux]"
            else:
                tag = f"[{self.messages.read_only_tag}]"
            project = info["project"]
            lines.append(f"  /{i} {self._short_path(project)} {tag}{marker}")
        lines.append(f"\n{self.messages.active_marker}")
        self._xmpp_send("\n".join(lines))

    async def _cmd_help(self) -> None:
        self._xmpp_send(self.messages.help_text)

    async def _send_to_session_by_index(self, index: int, text: str) -> None:
        sid, info = self.registry.get_by_index(index)
        if not info:
            self._xmpp_send(self.messages.session_not_found.format(index=index))
            return
        if sid is None:
            return
        self.registry.set_active(sid)
        project = self._short_path(info["project"])
        if not info["backend"]:
            self._xmpp_send(self.messages.no_backend.format(project=project))
            return
        ok = await self._stuff_to_session(info, text)
        if ok:
            if not self._xmpp_send(f"→ [{project}] {self.messages.sent}"):
                log.warning("Sent to session but XMPP confirmation failed (project=%s)", project)
        else:
            self._xmpp_send(self.messages.delivery_failed.format(project=project))

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
        project = self._short_path(info["project"])
        if not info["backend"]:
            self._xmpp_send(self.messages.no_backend.format(project=project))
            return
        ok = await self._stuff_to_session(info, text)
        if ok:
            if not self._xmpp_send(f"→ [{project}] {self.messages.sent}"):
                log.warning("Sent to session but XMPP confirmation failed (project=%s)", project)
        else:
            self._xmpp_send(self.messages.delivery_failed.format(project=project))

    async def _stuff_to_session(self, info: SessionInfo, text: str) -> bool:
        mux = get_multiplexer(info["backend"])
        if not mux:
            log.warning("No backend for session (project=%s)", info["project"])
            return False
        return await mux.send_text(info["sty"], info["window"], text)

    def _xmpp_send(self, text: str) -> bool:
        return self.xmpp.send(self.config.recipient, text)

    @staticmethod
    def _short_path(path: str) -> str:
        """Shorten path by replacing home directory with ~."""
        home = str(Path.home())
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~" + path[len(home):]
        return path

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
            *cmd_parts, sty,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0

    async def _cleanup_stale_sessions(self) -> int:
        """Remove dead sessions and deduplicate (keep newest per key)."""
        to_remove: set[str] = set()

        # 1. Remove sessions whose multiplexer is dead (check in parallel)
        items = list(self.registry.sessions.items())
        if items:
            alive_results = await asyncio.gather(
                *(self._is_session_alive(info) for _, info in items)
            )
            for (sid, _), alive in zip(items, alive_results, strict=True):
                if not alive:
                    to_remove.add(sid)

        # 2. Deduplicate — keep only the newest per project and per sty+window
        for key_fn in (
            lambda info: info["project"],
            lambda info: (info["sty"], info["window"]) if info["sty"] and info["window"] else None,
        ):
            groups: dict[object, list[tuple[str, float]]] = {}
            for sid, info in self.registry.sessions.items():
                if sid in to_remove:
                    continue
                key = key_fn(info)
                if key is None:
                    continue
                groups.setdefault(key, []).append((sid, info["registered_at"]))
            for entries in groups.values():
                if len(entries) > 1:
                    entries.sort(key=lambda e: e[1])  # oldest first
                    for sid, _ in entries[:-1]:  # remove all but newest
                        to_remove.add(sid)

        for sid in to_remove:
            stale = self.registry.sessions.get(sid)
            project = stale["project"] if stale else "?"
            self.registry.unregister(sid)
            log.info("Cleaned stale session %s (project=%s)", sid, project)
        if to_remove:
            log.info(self.messages.stale_sessions_cleaned.format(count=len(to_remove)))
        return len(to_remove)

    # --- Socket request handling ---

    async def _handle_request(self, req: dict[str, object]) -> dict[str, object]:
        cmd = str(req.get("cmd", ""))

        if cmd == "register":
            return self._handle_register(req)
        elif cmd == "unregister":
            return self._handle_unregister(req)
        elif cmd == "send":
            message = str(req.get("message", ""))
            if message:
                sent = self._xmpp_send(message)
                return {"ok": sent}
            return {"ok": True}
        elif cmd == "response":
            return self._handle_response(req)
        elif cmd == "query":
            return self._handle_query(req)
        else:
            return {"error": f"unknown command: {cmd}"}

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

            # Deduplicate: remove old sessions with same project or same sty+window
            for old_sid, old_info in list(self.registry.sessions.items()):
                if old_sid == sid:
                    continue
                if old_info["project"] == project:
                    log.info("Replacing stale session %s (same project=%s)", old_sid, project)
                    self.registry.unregister(old_sid)
                elif sty and window and old_info["sty"] == sty and old_info["window"] == window:
                    log.info("Replacing stale session %s (same sty=%s, window=%s)", old_sid, sty, window)
                    self.registry.unregister(old_sid)

            self.registry.register(
                session_id=sid,
                sty=sty,
                window=window,
                project=project,
                backend=backend,
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
        self.registry.unregister(str(sid))
        return {"ok": True}

    def _handle_response(self, req: dict[str, object]) -> dict[str, object]:
        session_id = str(req.get("session_id", ""))
        message = str(req.get("message", ""))
        if message:
            info = self.registry.get(session_id)
            if info:
                project = self._short_path(info["project"])
            elif "project" in req:
                project = self._short_path(str(req["project"]))
            else:
                project = "?"
            self._xmpp_send(f"[{project}] {message}")
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
        self._xmpp_send(self.messages.bridge_started)
        log.info("Bridge running. Press Ctrl+C to stop.")

        await stop.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        """Gracefully shut down all components."""
        log.info("Shutting down...")
        await self.socket_server.stop()
        self._xmpp_send(self.messages.bridge_stopped)
        await asyncio.sleep(1)
        self.xmpp.disconnect()
        self.registry.close()
        log.info("Bye.")
