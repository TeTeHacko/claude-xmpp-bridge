"""Unix socket server with JSON protocol for bridge communication."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket as _socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

MAX_REQUEST_SIZE = 65536


class SocketServer:
    """Unix socket server for the bridge daemon."""

    def __init__(
        self,
        socket_path: Path,
        request_handler: Callable[[dict[str, object]], Awaitable[dict[str, object]]],
        socket_token: str | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._request_handler = request_handler
        self._socket_token = socket_token
        self._server: asyncio.AbstractServer | None = None
        self._owns_socket = False

    async def start(self) -> None:
        """Start the socket server. Exits if another bridge is running."""
        if self._is_socket_alive():
            log.error(
                "Another bridge is already running on %s. "
                "If the previous process crashed, remove the stale socket: rm %s",
                self.socket_path,
                self.socket_path,
            )
            sys.exit(1)
        # Remove stale socket file if present (missing_ok avoids TOCTOU race)
        self.socket_path.unlink(missing_ok=True)

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Use umask for race-free permission setting
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        finally:
            os.umask(old_umask)

        self._owns_socket = True
        log.info("Socket server listening on %s", self.socket_path)

    def _is_socket_alive(self) -> bool:
        """Check if there's a live process on the socket."""
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(str(self.socket_path))
            return True
        except OSError:
            return False

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(MAX_REQUEST_SIZE), timeout=5)
            if not data:
                return
            line = data.decode("utf-8").strip()
            if not line:
                return

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                writer.write(json.dumps({"error": "invalid JSON"}).encode() + b"\n")
                await writer.drain()
                return

            if not isinstance(request, dict):
                writer.write(json.dumps({"error": "request must be a JSON object"}).encode() + b"\n")
                await writer.drain()
                return

            # Token authentication: if a socket_token is configured, every request
            # must include a matching "token" field. This prevents unauthorized
            # local processes (compromised subprocesses, third-party hooks) from
            # interacting with the bridge socket.
            if self._socket_token is not None:
                provided = request.get("token")
                if provided != self._socket_token:
                    log.warning("Socket request rejected: invalid or missing token")
                    writer.write(json.dumps({"error": "unauthorized"}).encode() + b"\n")
                    await writer.drain()
                    return

            response = await self._request_handler(request)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except TimeoutError:
            log.warning("Client read timeout")
        except Exception as e:
            log.error("Client handler error: %s", e)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def stop(self) -> None:
        """Stop the server and clean up the socket file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._owns_socket and self.socket_path.exists():
            self.socket_path.unlink()
