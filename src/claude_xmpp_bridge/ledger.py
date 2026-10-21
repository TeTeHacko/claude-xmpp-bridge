"""What the bridge actually delivered - so a receiver can verify a message.

A terminal session cannot tell bridge-injected text from its human typing,
and anything that runs as the same user can type into a screen window
directly. The ledger is the reference: ``verify <message_id>`` returns the
origin (``human`` = XMPP from the configured recipient, ``agent`` = socket/MCP,
i.e. any local process), the self-declared sender and the exact body the
bridge sent. Text in a prompt with an unknown id, or a body that differs from
the ledger, did not come from the bridge as shown.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .messages import body_sha256

# Long enough to verify anything still in a scrollback, short enough that the
# table holding message bodies does not grow without bound.
RETENTION_SECONDS = 7 * 24 * 3600


class MessageLedger:
    def __init__(self, db_path: Path | str) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS delivered_messages (
                message_id TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                from_session TEXT,
                from_label TEXT,
                from_jid TEXT,
                to_session TEXT,
                msg_type TEXT,
                body_sha256 TEXT NOT NULL,
                body TEXT NOT NULL,
                ts REAL NOT NULL
            )"""
        )
        self._db.commit()

    def record(
        self,
        *,
        message_id: str,
        origin: str,
        body: str,
        to_session: str | None,
        msg_type: str,
        from_session: str | None = None,
        from_label: str | None = None,
        from_jid: str | None = None,
    ) -> None:
        now = time.time()
        self._db.execute(
            "INSERT OR REPLACE INTO delivered_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                origin,
                from_session,
                from_label,
                from_jid,
                to_session,
                msg_type,
                body_sha256(body),
                body,
                now,
            ),
        )
        self._db.execute("DELETE FROM delivered_messages WHERE ts < ?", (now - RETENTION_SECONDS,))
        self._db.commit()

    def lookup(self, message_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM delivered_messages WHERE message_id = ?", (message_id.strip(),)
        ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._db.close()
