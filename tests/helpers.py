"""Shared test helper functions (not fixtures)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


def make_slixmpp_message(from_bare: str, body: str, mtype: str = "chat") -> MagicMock:
    """Create a fake slixmpp Message object for use in tests."""
    msg = MagicMock()
    msg.__getitem__ = lambda self, key: {
        "type": mtype,
        "from": MagicMock(bare=from_bare),
        "body": body,
    }[key]
    return msg


def make_mock_conn(MockXMPP) -> tuple[MagicMock, dict]:
    """Create a mock XMPPConnection and capture the on_message callback.

    Returns (conn, captured) where captured["cb"] holds the registered callback
    after XMPPBridge.__init__ has been called.
    """
    conn = MagicMock()
    conn.connected = asyncio.Event()
    conn.connected.set()
    captured: dict = {}

    def _capture(cb):
        captured["cb"] = cb

    conn.on_message.side_effect = _capture
    MockXMPP.return_value = conn
    return conn, captured
