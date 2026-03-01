"""Socket client for communicating with the bridge daemon."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_SOCKET_PATH

log = logging.getLogger(__name__)


def send_to_bridge(
    request: dict[str, object],
    socket_path: Path = DEFAULT_SOCKET_PATH,
) -> dict[str, object] | None:
    """Send JSON request to bridge socket. Returns response dict or None on failure."""
    if not socket_path.exists():
        return None

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
    """Send via claude-xmpp-notify as fallback when bridge is not running."""
    try:
        subprocess.run(  # noqa: S603
            ["claude-xmpp-notify", message],  # noqa: S607
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # Also try the legacy command name
        try:
            subprocess.run(  # noqa: S603
                ["xmpp-notify", message],  # noqa: S607
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
