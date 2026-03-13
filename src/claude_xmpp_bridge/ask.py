"""Send XMPP message and wait for a reply."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import slixmpp

from .config import NotifyConfig
from .xmpp import XMPPConnection

DEFAULT_TIMEOUT = 300
DEFAULT_CONNECTION_TIMEOUT = 30.0
DEFAULT_DISCONNECT_GRACE = 1.0

log = logging.getLogger(__name__)


async def send_and_wait(
    config: NotifyConfig,
    message: str,
    timeout: int | float = DEFAULT_TIMEOUT,
    *,
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
    disconnect_grace: float = DEFAULT_DISCONNECT_GRACE,
) -> str | None:
    """Send a message and wait for a reply from the recipient. Returns reply text or None on timeout."""
    reply_text: str | None = None
    got_reply = asyncio.Event()

    async def on_message(msg: slixmpp.Message) -> None:
        nonlocal reply_text
        if msg["type"] not in ("chat", "normal"):
            return
        sender = msg["from"].bare
        if sender != config.recipient:
            log.warning("Ignored XMPP reply from unexpected sender: %s", sender)
            return
        reply_text = msg["body"].strip()
        got_reply.set()

    conn = XMPPConnection(config.jid, config.password)
    conn.on_message(on_message)
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

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(got_reply.wait(), timeout=timeout)
    finally:
        conn.disconnect()
        if disconnect_grace > 0:
            await asyncio.sleep(disconnect_grace)

    return reply_text
