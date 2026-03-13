"""Fire-and-forget XMPP message sending."""

from __future__ import annotations

import asyncio

from .config import NotifyConfig
from .xmpp import XMPPConnection

DEFAULT_CONNECTION_TIMEOUT = 30
DEFAULT_DISCONNECT_GRACE = 1.0


async def send_notification(
    config: NotifyConfig,
    message: str,
    *,
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
    disconnect_grace: float = DEFAULT_DISCONNECT_GRACE,
) -> None:
    """Connect, send a single message, and disconnect."""
    conn = XMPPConnection(config.jid, config.password)
    try:
        conn.start()
        try:
            await asyncio.wait_for(conn.connected.wait(), timeout=connection_timeout)
        except TimeoutError:
            raise ConnectionError(
                f"XMPP connection timeout ({connection_timeout}s)"
                " — server may be unavailable"
            ) from None
        if not conn.send(config.recipient, message):
            raise ConnectionError("XMPP send failed — not connected")
        if disconnect_grace > 0:
            await asyncio.sleep(disconnect_grace)
    finally:
        conn.disconnect()
