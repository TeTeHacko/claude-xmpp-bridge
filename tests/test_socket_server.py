"""Tests for the Unix socket server."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from claude_xmpp_bridge.socket_server import SocketServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_request(socket_path: Path, request: dict | str | bytes) -> dict:
    """Connect to the Unix socket, send a request, and return parsed response."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        if isinstance(request, bytes):
            payload = request
        elif isinstance(request, str):
            payload = request.encode()
        else:
            payload = json.dumps(request).encode()
        writer.write(payload + b"\n")
        await writer.drain()
        writer.write_eof()

        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        return json.loads(data.decode().strip())
    finally:
        writer.close()
        await writer.wait_closed()


class MockHandler:
    """Records calls and returns configurable responses."""

    def __init__(self, response: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or {"status": "ok"}

    async def __call__(self, request: dict) -> dict:
        self.calls[len(self.calls):] = [request]  # append
        return self._response


# ---------------------------------------------------------------------------
# 1. Start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_creates_socket(self, socket_path: Path) -> None:
        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            assert socket_path.exists()
            assert stat.S_ISSOCK(socket_path.stat().st_mode)
        finally:
            await server.stop()

    async def test_stop_removes_socket(self, socket_path: Path) -> None:
        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        assert socket_path.exists()

        await server.stop()
        assert not socket_path.exists()

    async def test_start_replaces_stale_socket(self, socket_path: Path) -> None:
        """If a stale socket file exists (no live process), start replaces it."""
        # Create a stale socket file (just a regular file pretending)
        socket_path.touch()

        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            assert socket_path.exists()
            # Server should be functional
            resp = await _send_request(socket_path, {"command": "ping"})
            assert resp["status"] == "ok"
        finally:
            await server.stop()

    async def test_stop_idempotent(self, socket_path: Path) -> None:
        """Calling stop on a never-started server does not raise."""
        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.stop()  # should not raise


# ---------------------------------------------------------------------------
# 2. Register command
# ---------------------------------------------------------------------------


class TestRegisterCommand:
    async def test_register(self, socket_path: Path) -> None:
        handler = MockHandler({"status": "ok"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            request = {
                "command": "register",
                "session_id": "sess-1",
                "screen_session": "12345.pts-0",
                "screen_window": "3",
                "project": "/home/user/project",
            }
            resp = await _send_request(socket_path, request)
            assert resp["status"] == "ok"
            assert len(handler.calls) == 1
            assert handler.calls[0]["command"] == "register"
            assert handler.calls[0]["session_id"] == "sess-1"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 3. Unregister command
# ---------------------------------------------------------------------------


class TestUnregisterCommand:
    async def test_unregister(self, socket_path: Path) -> None:
        handler = MockHandler({"status": "ok"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            request = {"command": "unregister", "session_id": "sess-1"}
            resp = await _send_request(socket_path, request)
            assert resp["status"] == "ok"
            assert len(handler.calls) == 1
            assert handler.calls[0]["command"] == "unregister"
            assert handler.calls[0]["session_id"] == "sess-1"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 4. Send command
# ---------------------------------------------------------------------------


class TestSendCommand:
    async def test_send(self, socket_path: Path) -> None:
        handler = MockHandler({"status": "ok"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            request = {
                "command": "send",
                "session_id": "sess-1",
                "message": "Build finished.",
            }
            resp = await _send_request(socket_path, request)
            assert resp["status"] == "ok"
            assert len(handler.calls) == 1
            assert handler.calls[0]["command"] == "send"
            assert handler.calls[0]["message"] == "Build finished."
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 5. Response command
# ---------------------------------------------------------------------------


class TestResponseCommand:
    async def test_response(self, socket_path: Path) -> None:
        handler = MockHandler({"status": "ok"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            request = {
                "command": "response",
                "session_id": "sess-1",
                "text": "Task completed successfully.",
            }
            resp = await _send_request(socket_path, request)
            assert resp["status"] == "ok"
            assert len(handler.calls) == 1
            assert handler.calls[0]["command"] == "response"
            assert handler.calls[0]["text"] == "Task completed successfully."
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 6. Invalid JSON
# ---------------------------------------------------------------------------


class TestInvalidJSON:
    async def test_invalid_json_returns_error(self, socket_path: Path) -> None:
        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            resp = await _send_request(socket_path, b"not valid json {{{")
            assert "error" in resp
            assert "invalid JSON" in resp["error"]
            # Handler should NOT have been called
            assert len(handler.calls) == 0
        finally:
            await server.stop()

    async def test_truncated_json_returns_error(self, socket_path: Path) -> None:
        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            resp = await _send_request(socket_path, b'{"command": "send",')
            assert "error" in resp
            assert len(handler.calls) == 0
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 7. Unknown command
# ---------------------------------------------------------------------------


class TestUnknownCommand:
    async def test_unknown_command_forwarded_to_handler(self, socket_path: Path) -> None:
        """The server itself does not validate commands -- it delegates to the handler.

        If the handler returns an error for unknown commands, the client gets it.
        """
        handler = MockHandler({"status": "error", "error": "unknown command"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            request = {"command": "nonexistent"}
            resp = await _send_request(socket_path, request)
            assert resp["status"] == "error"
            assert "unknown command" in resp["error"]
            assert len(handler.calls) == 1
            assert handler.calls[0]["command"] == "nonexistent"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 8. Missing required fields (handler returns error)
# ---------------------------------------------------------------------------


class TestMissingFields:
    async def test_missing_fields_handler_error(self, socket_path: Path) -> None:
        """When a required field is missing, the handler returns an error."""
        handler = MockHandler({"status": "error", "error": "missing field: session_id"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            request = {"command": "register"}  # missing session_id etc.
            resp = await _send_request(socket_path, request)
            assert resp["status"] == "error"
            assert "missing field" in resp["error"]
            assert len(handler.calls) == 1
        finally:
            await server.stop()

    async def test_handler_exception_does_not_crash_server(self, socket_path: Path) -> None:
        """If the handler raises, the server logs the error and stays alive."""
        call_count = 0

        async def failing_handler(request: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            return {"status": "ok"}

        server = SocketServer(socket_path, failing_handler)
        await server.start()
        try:
            # First request -- handler raises
            reader1, writer1 = await asyncio.open_unix_connection(str(socket_path))
            writer1.write(json.dumps({"command": "bad"}).encode() + b"\n")
            await writer1.drain()
            writer1.write_eof()
            # The server catches the exception; client may get no response or connection closed
            await asyncio.wait_for(reader1.read(65536), timeout=2)
            writer1.close()
            await writer1.wait_closed()

            # Second request should still work -- server did not crash
            resp = await _send_request(socket_path, {"command": "ok"})
            assert resp["status"] == "ok"
            assert call_count == 2
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 9. Socket permissions
# ---------------------------------------------------------------------------


class TestSocketPermissions:
    async def test_socket_has_restricted_permissions(self, socket_path: Path) -> None:
        """Socket file should be created with 0o600 permissions (via umask 0o177)."""
        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            mode = socket_path.stat().st_mode
            # Extract the permission bits (lower 12 bits)
            perms = stat.S_IMODE(mode)
            assert perms == 0o600, f"Expected 0o600, got {oct(perms)}"
        finally:
            await server.stop()

    async def test_umask_restored_after_start(self, socket_path: Path) -> None:
        """The original umask should be restored after the server starts."""
        import os

        original = os.umask(0o022)
        os.umask(original)  # restore immediately to read it

        handler = MockHandler()
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            current = os.umask(0o022)
            os.umask(current)
            assert current == original, (
                f"umask not restored: expected {oct(original)}, got {oct(current)}"
            )
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# Extra: concurrent clients
# ---------------------------------------------------------------------------


class TestConcurrentClients:
    async def test_multiple_clients_sequential(self, socket_path: Path) -> None:
        handler = MockHandler({"status": "ok"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            for i in range(5):
                resp = await _send_request(
                    socket_path,
                    {"command": "send", "n": i},
                )
                assert resp["status"] == "ok"
            assert len(handler.calls) == 5
        finally:
            await server.stop()

    async def test_multiple_clients_concurrent(self, socket_path: Path) -> None:
        handler = MockHandler({"status": "ok"})
        server = SocketServer(socket_path, handler)
        await server.start()
        try:
            tasks = [
                _send_request(socket_path, {"command": "send", "n": i})
                for i in range(10)
            ]
            results = await asyncio.gather(*tasks)
            assert all(r["status"] == "ok" for r in results)
            assert len(handler.calls) == 10
        finally:
            await server.stop()
