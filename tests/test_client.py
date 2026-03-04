"""Tests for claude_xmpp_bridge.client — socket client and fallback notify."""

from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import call, patch

import pytest

from claude_xmpp_bridge.client import fallback_notify, send_to_bridge

# ---------------------------------------------------------------------------
# send_to_bridge — running bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_bridge_running(socket_path):
    """Bridge is running — send request, get JSON response back."""
    response_payload = {"status": "ok", "session_id": "abc123"}

    async def handler(reader, writer):
        data = await reader.read(65536)
        request = json.loads(data.decode())
        assert request["command"] == "register"
        writer.write(json.dumps(response_payload).encode())
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    try:
        # Run sync client in a thread to avoid blocking the event loop
        result = await asyncio.to_thread(send_to_bridge, {"command": "register"}, socket_path)
        assert result == response_payload
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_send_to_bridge_empty_response(socket_path):
    """Bridge accepts connection but sends empty response — returns None."""

    async def handler(reader, writer):
        await reader.readline()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    try:
        result = send_to_bridge({"command": "ping"}, socket_path=socket_path)
        assert result is None
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# send_to_bridge — no bridge (socket doesn't exist)
# ---------------------------------------------------------------------------


def test_send_to_bridge_no_socket(socket_path):
    """Socket file does not exist — returns None immediately."""
    assert not socket_path.exists()
    result = send_to_bridge({"command": "register"}, socket_path=socket_path)
    assert result is None


# ---------------------------------------------------------------------------
# send_to_bridge — stale socket (file exists, nobody listening)
# ---------------------------------------------------------------------------


def test_send_to_bridge_stale_socket(socket_path):
    """Socket file exists but no server is listening — returns None (OSError)."""
    # Create a regular file pretending to be a socket
    socket_path.touch()
    result = send_to_bridge({"command": "register"}, socket_path=socket_path)
    assert result is None


def test_send_to_bridge_stale_after_close(socket_path):
    """Socket file exists but refers to a closed Unix socket — returns None."""
    import socket as _socket

    # Create a real Unix socket, bind it, then close — leaving a stale file
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    sock.listen(1)
    sock.close()

    assert socket_path.exists()
    result = send_to_bridge({"command": "ping"}, socket_path=socket_path)
    assert result is None


# ---------------------------------------------------------------------------
# fallback_notify — subprocess mock
# ---------------------------------------------------------------------------


def test_fallback_notify_primary_succeeds():
    """claude-xmpp-notify succeeds — message passed via stdin, not CLI arg."""
    with patch("claude_xmpp_bridge.client.subprocess.run") as mock_run:
        fallback_notify("test message")

        mock_run.assert_called_once_with(
            ["claude-xmpp-notify"],
            input=b"test message",
            check=True,
            timeout=30,
        )


def test_fallback_notify_falls_back_to_xmpp_notify():
    """claude-xmpp-notify not found — falls back to xmpp-notify."""
    with patch("claude_xmpp_bridge.client.subprocess.run") as mock_run:
        mock_run.side_effect = [
            FileNotFoundError(),  # claude-xmpp-notify missing
            None,  # xmpp-notify succeeds
        ]
        fallback_notify("test message")

        assert mock_run.call_count == 2
        mock_run.assert_has_calls(
            [
                call(["claude-xmpp-notify"], input=b"test message", check=True, timeout=30),
                call(["xmpp-notify"], input=b"test message", check=True, timeout=30),
            ]
        )


def test_fallback_notify_falls_back_on_calledprocesserror():
    """claude-xmpp-notify fails with non-zero exit — falls back to xmpp-notify."""
    with patch("claude_xmpp_bridge.client.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "claude-xmpp-notify"),
            None,
        ]
        fallback_notify("test message")

        assert mock_run.call_count == 2


def test_fallback_notify_both_fail_file_not_found(capsys):
    """Both commands missing — prints specific 'command not found' message."""
    with (
        patch("claude_xmpp_bridge.client.subprocess.run") as mock_run,
        patch("claude_xmpp_bridge.client.sys.exit", side_effect=SystemExit(1)) as mock_exit,
    ):
        mock_run.side_effect = [
            FileNotFoundError(),  # claude-xmpp-notify missing
            FileNotFoundError(),  # xmpp-notify also missing
        ]

        with pytest.raises(SystemExit):
            fallback_notify("test message")

        assert mock_run.call_count == 2
        mock_exit.assert_called_once_with(1)
        captured = capsys.readouterr()
        assert "command not found" in captured.err


def test_fallback_notify_both_fail_timeout(capsys):
    """Both commands timeout — prints specific timeout message."""
    with (
        patch("claude_xmpp_bridge.client.subprocess.run") as mock_run,
        patch("claude_xmpp_bridge.client.sys.exit", side_effect=SystemExit(1)) as mock_exit,
    ):
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude-xmpp-notify", 30),
            subprocess.TimeoutExpired("xmpp-notify", 30),
        ]

        with pytest.raises(SystemExit):
            fallback_notify("test message")

        mock_exit.assert_called_once_with(1)
        captured = capsys.readouterr()
        assert "timed out" in captured.err


def test_fallback_notify_both_fail_exit_code(capsys):
    """Both commands fail with non-zero exit — prints exit code."""
    with (
        patch("claude_xmpp_bridge.client.subprocess.run") as mock_run,
        patch("claude_xmpp_bridge.client.sys.exit", side_effect=SystemExit(1)) as mock_exit,
    ):
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "claude-xmpp-notify"),
            subprocess.CalledProcessError(2, "xmpp-notify"),
        ]

        with pytest.raises(SystemExit):
            fallback_notify("test message")

        mock_exit.assert_called_once_with(1)
        captured = capsys.readouterr()
        assert "exited with code 2" in captured.err


def test_fallback_notify_primary_timeout_falls_back():
    """claude-xmpp-notify times out — falls back to xmpp-notify."""
    with patch("claude_xmpp_bridge.client.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude-xmpp-notify", 30),
            None,
        ]
        fallback_notify("test message")

        assert mock_run.call_count == 2
