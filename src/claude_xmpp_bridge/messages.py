"""Configurable UI messages with English defaults and TOML override support."""

from __future__ import annotations

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
    msgs = Messages()
    if path is None:
        return msgs

    if not path.is_file():
        return msgs

    with open(path, "rb") as f:
        data = tomllib.load(f)

    msg_fields = {f.name for f in fields(Messages)}
    for key, value in data.items():
        if key in msg_fields and isinstance(value, str):
            object.__setattr__(msgs, key, value)

    return msgs
