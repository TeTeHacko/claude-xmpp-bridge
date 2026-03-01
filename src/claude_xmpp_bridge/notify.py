"""Fire-and-forget XMPP message sending."""

from __future__ import annotations

import asyncio

from .config import NotifyConfig
from .xmpp import XMPPConnection


async def send_notification(config: NotifyConfig, message: str) -> None:
    """Connect, send a single message, and disconnect."""
    conn = XMPPConnection(config.jid, config.password)
    try:
        conn.start()
        try:
            await asyncio.wait_for(conn.connected.wait(), timeout=30)
        except TimeoutError:
            raise ConnectionError(
                "XMPP connection timeout (30s) — server may be unavailable"
            ) from None
        if not conn.send(config.recipient, message):
            raise ConnectionError("XMPP send failed — not connected")
        await asyncio.sleep(1)
    finally:
        conn.disconnect()
