"""Configurable UI messages with English defaults and TOML override support."""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Messages:
    """All user-facing text strings. Override via TOML file."""

    bridge_started: str = "XMPP Bridge started."
    bridge_stopped: str = "XMPP Bridge stopped."
    no_sessions: str = "No active sessions."
    session_list_header: str = "Sessions:"
    active_marker: str = "* = active session"
    sent: str = "sent"
    delivery_failed: str = "Delivery to [{project}] failed"
    no_backend: str = "Session [{project}] has no multiplexer — cannot deliver message"
    session_not_found: str = "Session #{index} not found. Type /list."
    no_active_session: str = "No active session. Type /list."
    unknown_command: str = "Unknown command: {cmd}\nType /help for help."
    usage_send_to: str = "Usage: {cmd} <message>"
    help_text: str = field(
        default=(
            "XMPP Bridge commands:\n"
            "  /list, /l  - list sessions\n"
            "  /N message - send to session #N\n"
            "  /help      - this help\n"
            "  text       - send to last active session"
        )
    )
    read_only_tag: str = "read-only"
    stale_sessions_cleaned: str = "Cleaned {count} stale session(s)"
    relay_no_target: str = "relay requires 'to' (session_id or index)"
    relay_target_not_found: str = "Relay target session not found"
    relay_no_backend: str = "Relay target [{project}] has no multiplexer"
    relay_delivered: str = "relay → {target_prefix}"
    relay_failed: str = "Relay to [{project}] failed"
    broadcast_no_message: str = "broadcast requires 'message'"
    broadcast_sent: str = "broadcast → {count} session(s)"
    mcp_started: str = "MCP server listening on http://127.0.0.1:{port}/mcp"
    mcp_stopped: str = "MCP server stopped"
    mcp_send_missing_to: str = "send_message requires 'to' (session_id)"
    mcp_send_missing_message: str = "send_message requires 'message'"
    mcp_send_target_not_found: str = "Target session not found: {to}"
    mcp_send_no_backend: str = "Target session [{project}] has no multiplexer"
    mcp_send_failed: str = "Delivery to [{project}] failed"
    mcp_send_ok: str = "Message delivered to {target_prefix}"


def load_messages(path: Path | None = None) -> Messages:
    """Load messages, optionally overriding defaults from a TOML file."""
    if path is None or not path.is_file():
        return Messages()

    with open(path, "rb") as f:
        data = tomllib.load(f)

    msg_fields = {f.name for f in fields(Messages)}
    overrides = {k: v for k, v in data.items() if k in msg_fields and isinstance(v, str)}
    return Messages(**overrides)


def format_generated_agent_message(
    *,
    msg_type: str,
    message: str,
    from_session_id: str | None = None,
    to_session_id: str | None = None,
    mode: str | None = None,
    message_id: str | None = None,
) -> str:
    """Wrap inter-agent text in a clearly generated envelope with JSON metadata.

    The wrapper serves two purposes:
      1. Human readers in shared terminal windows can immediately see that the
         text was injected by the bridge rather than typed by a human.
      2. Agents can parse the JSON line if they need structured metadata such as
         relay/broadcast mode or sender session ID.

    If *message* is already wrapped in this format, it is returned unchanged so
    relay+nudge/inbox paths do not double-wrap the payload.
    """
    if message.startswith("[bridge-generated message]\n"):
        lines = message.splitlines()
        if len(lines) >= 2:
            try:
                meta = json.loads(lines[1])
            except json.JSONDecodeError:
                meta = None
            if isinstance(meta, dict) and meta.get("generated") is True:
                return message

    meta = {
        "type": msg_type,
        "generated": True,
        "from": from_session_id,
        "to": to_session_id,
        "mode": mode,
        "message_id": message_id,
        "ts": time.time(),
    }
    return "[bridge-generated message]\n" + json.dumps(meta, ensure_ascii=False) + "\n\n" + message
