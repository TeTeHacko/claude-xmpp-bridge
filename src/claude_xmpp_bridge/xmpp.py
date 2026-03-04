"""Shared slixmpp wrapper with reconnect and exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import slixmpp

log = logging.getLogger(__name__)

# Backoff parameters
BACKOFF_INITIAL = 5.0
BACKOFF_MULTIPLIER = 2.0
BACKOFF_MAX = 60.0


class XMPPConnection:
    """Wrapper around slixmpp.ClientXMPP with reconnect support."""

    def __init__(self, jid: str, password: str) -> None:
        self.jid = jid
        self._password = password
        self.connected = asyncio.Event()
        self._bot: slixmpp.ClientXMPP | None = None
        self._message_callback: Callable[[slixmpp.Message], Awaitable[None]] | None = None
        self._backoff = BACKOFF_INITIAL
        self._should_reconnect = True

    def on_message(self, callback: Callable[[slixmpp.Message], Awaitable[None]]) -> None:
        """Set the incoming message callback."""
        self._message_callback = callback

    def __repr__(self) -> str:
        return f"XMPPConnection(jid={self.jid!r})"

    def start(self) -> None:
        """Create and connect the XMPP client."""
        self._bot = slixmpp.ClientXMPP(self.jid, self._password)
        self._bot.add_event_handler("session_start", self._on_session_start)
        self._bot.add_event_handler("message", self._on_message)
        self._bot.add_event_handler("disconnected", self._on_disconnected)
        self._bot.connect()
        log.info("XMPP connecting as %s", self.jid)

    async def _on_session_start(self, _event: object) -> None:
        if self._bot:
            self._bot.send_presence()
        self._backoff = BACKOFF_INITIAL  # Reset on successful connect
        self.connected.set()
        log.info("XMPP connected")

    async def _on_message(self, msg: slixmpp.Message) -> None:
        if self._message_callback:
            await self._message_callback(msg)

    async def _on_disconnected(self, _event: object) -> None:
        self.connected.clear()
        if not self._should_reconnect:
            return
        delay = self._backoff
        log.warning("XMPP disconnected, reconnecting in %.0fs...", delay)
        self._backoff = min(self._backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX)
        await asyncio.sleep(delay)
        if self._bot and self._should_reconnect:
            self._bot.connect()

    def send(self, recipient: str, text: str) -> bool:
        """Send a plaintext message. Returns True if sent, False if not connected."""
        if not self._bot or not self.connected.is_set():
            log.warning("XMPP not connected, dropping: %s", text[:100])
            return False
        msg = self._bot.make_message(mto=recipient, mbody=text, mtype="chat")
        del msg["html"]
        msg.send()
        return True

    def disconnect(self) -> None:
        """Disconnect the XMPP client."""
        self._should_reconnect = False
        if self._bot:
            self._bot.disconnect()
            self._bot = None
