"""Socket client for communicating with the bridge daemon."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_SOCKET_PATH

log = logging.getLogger(__name__)

# Read socket token from environment variable or default token file.
# The token is injected into every request so the bridge can authenticate callers.
_TOKEN_FILE = Path.home() / ".config" / "claude-xmpp-bridge" / "socket_token"


def _get_socket_token() -> str | None:
    """Return the socket token from env var or token file, or None if not configured."""
    env_token = os.environ.get("CLAUDE_XMPP_SOCKET_TOKEN")
    if env_token:
        return env_token
    if _TOKEN_FILE.is_file():
        token = _TOKEN_FILE.read_text().strip()
        return token if token else None
    return None


def send_to_bridge(
    request: dict[str, object],
    socket_path: Path = DEFAULT_SOCKET_PATH,
) -> dict[str, object] | None:
    """Send JSON request to bridge socket. Returns response dict or None on failure."""
    if not socket_path.exists():
        return None

    # Attach token if available
    token = _get_socket_token()
    if token and "token" not in request:
        request = {**request, "token": token}

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(socket_path))
            sock.sendall(json.dumps(request).encode() + b"\n")
            sock.shutdown(socket.SHUT_WR)
            data = sock.recv(65536)
            if data:
                parsed = json.loads(data.decode())
                if isinstance(parsed, dict):
                    return parsed
            return None
    except (OSError, json.JSONDecodeError):
        return None


def fallback_notify(message: str) -> None:
    """Send via claude-xmpp-notify as fallback when bridge is not running.

    Message is passed via stdin (not CLI args) to avoid exposure in 'ps aux'.
    """
    encoded = message.encode()
    try:
        subprocess.run(  # noqa: S603
            ["claude-xmpp-notify"],  # noqa: S607
            input=encoded,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # Also try the legacy command name
        try:
            subprocess.run(  # noqa: S603
                ["xmpp-notify"],  # noqa: S607
                input=encoded,
                check=True,
                timeout=30,
            )
        except FileNotFoundError:
            print("xmpp-notify: command not found", file=sys.stderr)
            sys.exit(1)
        except subprocess.TimeoutExpired:
            print("xmpp-notify: timed out after 30s", file=sys.stderr)
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            print(f"xmpp-notify: exited with code {e.returncode}", file=sys.stderr)
            sys.exit(1)
