"""Configurable UI messages with English defaults and TOML override support."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Messages:
    """All user-facing text strings. Override via TOML file."""

    bridge_started: str = "XMPP Bridge started."
    bridge_stopped: str = "XMPP Bridge stopped."
    no_sessions: str = "No active sessions."
    session_list_header: str = "Sessions:"
    active_marker: str = "* = active session"
    sent: str = "sent"
    # Prepended to text a human sent over XMPP before it is pasted into a terminal,
    # so the agent (and anyone reading the scrollback) can tell it was not typed.
    # Empty string disables the marker.
    # ``{id}`` is replaced by the message id the bridge records (origin
    # "human"), so the session can run ``claude-xmpp-client verify <id>``.
    xmpp_inbound_prefix: str = "[via XMPP id={id}] "
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
    mcp_send_queued: str = "Message queued for {target_prefix}"
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


# ---------------------------------------------------------------------------
# Generated-message envelope
# ---------------------------------------------------------------------------
#
# Why the envelope is signed (2026-10-21): a terminal session cannot tell
# bridge-injected text from its human typing - Claude Code shows both as user
# input. One session attributed an agent's relay to tth for exactly that
# reason, and a relay body could even *start* with a hand-made envelope,
# which the old double-wrap guard passed through unchanged (forged "from").
# Now:
#   * ``origin`` says which channel the bridge received the text on -
#     "agent" (socket/MCP, i.e. any local process) or "human" (XMPP from the
#     configured recipient JID). Agents can never produce "human".
#   * ``from``/``from_label`` stay self-declared; ``from_verified`` is false.
#   * ``sig`` is an HMAC over the metadata + body with a key only the bridge
#     holds. A message is passed through un-rewrapped only with a valid sig;
#     anything else is wrapped again and its look-alike lines neutralised.
#   * the receiver verifies with ``claude-xmpp-client verify <message_id>``
#     (or the MCP tool ``verify_message``), which returns what the bridge
#     actually delivered - see ledger.py.

ENVELOPE_HEADER = "[bridge-generated message]"
ENVELOPE_VERSION = 2

# Lines in an untrusted body that could be mistaken for bridge or human
# markers get this prefix, so they read as quoted text.
_QUOTE_PREFIX = "│ "
_LOOKALIKE_RE = re.compile(r"^(\s*)(\[(?:bridge|via XMPP)\b)", re.IGNORECASE | re.MULTILINE)

_signing_key: bytes = secrets.token_bytes(32)


def set_signing_key(key: bytes) -> None:
    """Install the bridge's HMAC key (persisted by the bridge, 0600)."""
    global _signing_key
    if len(key) < 16:
        raise ValueError("signing key too short")
    _signing_key = key


def neutralize_untrusted(text: str) -> str:
    """Quote lines that look like a bridge envelope/stamp or an XMPP marker."""
    return _LOOKALIKE_RE.sub(lambda m: m.group(1) + _QUOTE_PREFIX + m.group(2), text)


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sign(meta: dict[str, Any], body: str) -> str:
    fields_ = {k: meta.get(k) for k in ("type", "origin", "from", "to", "mode", "message_id", "ts")}
    payload = json.dumps(fields_, sort_keys=True, ensure_ascii=False) + "\n" + body_sha256(body)
    return hmac.new(_signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _split_envelope(raw: str) -> tuple[dict[str, Any], str] | None:
    if not raw.startswith(ENVELOPE_HEADER + "\n"):
        return None
    lines = raw.split("\n", 3)  # header, json, blank, body
    if len(lines) < 2:
        return None
    try:
        meta = json.loads(lines[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict) or meta.get("generated") is not True:
        return None
    return meta, (lines[3] if len(lines) > 3 else "")


def envelope_is_authentic(raw: str) -> bool:
    """True only for an envelope this bridge produced (valid ``sig``)."""
    parts = _split_envelope(raw)
    if parts is None:
        return False
    meta, body = parts
    sig = meta.get("sig")
    return isinstance(sig, str) and hmac.compare_digest(sig, _sign(meta, body))


def format_generated_agent_message(
    *,
    msg_type: str,
    message: str,
    from_session_id: str | None = None,
    to_session_id: str | None = None,
    mode: str | None = None,
    message_id: str | None = None,
    origin: str = "agent",
    from_label: str | None = None,
) -> str:
    """Wrap inter-agent text in a signed, clearly generated envelope.

    An envelope this bridge already produced (valid ``sig``) is returned
    unchanged, so relay+nudge/inbox paths do not double-wrap. Anything else -
    including text that merely *looks* like an envelope - is wrapped as an
    untrusted body.
    """
    if envelope_is_authentic(message):
        return message
    body = neutralize_untrusted(message)
    meta: dict[str, Any] = {
        "type": msg_type,
        "generated": True,
        "v": ENVELOPE_VERSION,
        "origin": origin,
        "from": from_session_id,
        "from_label": from_label,
        "from_verified": False,
        "to": to_session_id,
        "mode": mode,
        "message_id": message_id,
        "ts": time.time(),
        "verify": f"claude-xmpp-client verify {message_id}" if message_id else None,
    }
    meta["sig"] = _sign(meta, body)
    return ENVELOPE_HEADER + "\n" + json.dumps(meta, ensure_ascii=False) + "\n\n" + body


def parse_generated_agent_message(raw: str) -> tuple[str, dict[str, Any]]:
    """Parse a bridge-generated envelope and return ``(text, metadata)``.

    Works for v1 (unsigned) and v2 envelopes; ``metadata["authentic"]`` says
    whether the signature checked out. Non-envelope text is returned whole
    with an empty metadata dict.
    """
    parts = _split_envelope(raw)
    if parts is None:
        return raw, {}
    meta, body = parts
    meta = dict(meta)
    meta["authentic"] = envelope_is_authentic(raw)
    return body, meta
