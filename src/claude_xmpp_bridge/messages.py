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
