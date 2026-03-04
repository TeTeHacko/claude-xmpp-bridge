"""Session registry with SQLite persistence and input validation."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)

# Validation patterns
SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
STY_RE = re.compile(r"^[a-zA-Z0-9_.\-]{0,128}$")  # no colon: prevents tmux session:window injection
WINDOW_RE = re.compile(r"^[0-9]{0,6}$")  # max 6 digits, empty string allowed (screen default)


class SessionInfo(TypedDict):
    """Type for session data stored in the registry."""

    sty: str
    window: str
    project: str
    backend: str | None
    source: str | None  # "opencode", None = Claude Code
    registered_at: float


def _validate_session_id(session_id: str) -> None:
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"Invalid session_id: {session_id!r} (must be 1-128 characters: letters, digits, underscore, hyphen)"
        )


def _validate_sty(sty: str) -> None:
    if not STY_RE.match(sty):
        raise ValueError(
            f"Invalid sty: {sty!r} (must contain only letters, digits, dots, colons, underscores, hyphens)"
        )


def _validate_window(window: str) -> None:
    if not WINDOW_RE.match(window):
        raise ValueError(f"Invalid window: {window!r} (must be a number or empty)")


class SessionRegistry:
    """Track active Claude sessions with SQLite persistence."""

    def __init__(self, db_path: Path | str) -> None:
        self.sessions: dict[str, SessionInfo] = {}
        self.last_active: str | None = None
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  session_id TEXT PRIMARY KEY,"
            "  sty TEXT,"
            "  window TEXT,"
            "  project TEXT,"
            "  backend TEXT,"
            "  registered_at REAL"
            ")"
        )
        self._db.commit()
        # Migration: add source column for existing databases that predate this field
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        if "source" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
            self._db.commit()
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load sessions and state from SQLite on startup."""
        for row in self._db.execute(
            "SELECT session_id, sty, window, project, backend, source, registered_at FROM sessions"
        ):
            self.sessions[row[0]] = {  # type: ignore[assignment]
                "sty": row[1],
                "window": row[2],
                "project": row[3],
                "backend": row[4],
                "source": row[5],
                "registered_at": row[6],
            }
        row = self._db.execute("SELECT value FROM state WHERE key = 'last_active'").fetchone()
        if row and row[0] in self.sessions:
            self.last_active = row[0]
        log.info("Loaded %d sessions from DB", len(self.sessions))

    def _save_session(self, session_id: str) -> None:
        info = self.sessions[session_id]
        self._db.execute(
            "INSERT OR REPLACE INTO sessions (session_id, sty, window, project, backend, source, registered_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                info["sty"],
                info["window"],
                info["project"],
                info["backend"],
                info["source"],
                info["registered_at"],
            ),
        )
        self._save_last_active()
        self._db.commit()

    def _delete_session(self, session_id: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self._save_last_active()
        self._db.commit()

    def _save_last_active(self) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('last_active', ?)",
            (self.last_active,),
        )

    def register(
        self,
        session_id: str,
        sty: str,
        window: str,
        project: str,
        backend: str | None = None,
        source: str | None = None,
        registered_at: float | None = None,
    ) -> None:
        """Register a new session. Validates all inputs.

        registered_at: if provided, preserves the original slot timestamp so that
        the session keeps its position in /list after a re-registration.
        """
        _validate_session_id(session_id)
        _validate_sty(sty)
        _validate_window(window)

        is_reregister = session_id in self.sessions
        self.sessions[session_id] = {  # type: ignore[assignment]
            "sty": sty,
            "window": window,
            "project": project,
            "backend": backend,
            "source": source,
            "registered_at": registered_at if registered_at is not None else time.time(),
        }
        # Don't flip last_active when the same session re-registers — it would
        # hijack plain-text routing away from whatever the user targeted last.
        if not is_reregister:
            self.last_active = session_id
        self._save_session(session_id)
        log.info("Registered session %s (backend=%s, source=%s)", session_id, backend, source)
        log.debug("Registered session %s (project=%s, backend=%s, source=%s)", session_id, project, backend, source)

    def unregister(self, session_id: str) -> None:
        """Unregister a session."""
        if session_id in self.sessions:
            info = self.sessions.pop(session_id)
            log.info("Unregistered session %s", session_id)
            log.debug("Unregistered session %s (project=%s)", session_id, info["project"])
            if self.last_active == session_id:
                if self.sessions:
                    self.last_active = max(
                        self.sessions,
                        key=lambda s: self.sessions[s]["registered_at"],
                    )
                else:
                    self.last_active = None
            self._delete_session(session_id)

    def get(self, session_id: str) -> SessionInfo | None:
        """Get session info by ID."""
        return self.sessions.get(session_id)

    def list_sessions(self) -> dict[str, SessionInfo]:
        """Get all sessions (returns a shallow copy to prevent external mutation)."""
        return dict(self.sessions)

    def get_by_index(self, index: int) -> tuple[str | None, SessionInfo | None]:
        """Get session by 1-based index (sorted by registration time)."""
        sorted_ids = sorted(
            self.sessions,
            key=lambda s: self.sessions[s]["registered_at"],
        )
        if 1 <= index <= len(sorted_ids):
            sid = sorted_ids[index - 1]
            return sid, self.sessions[sid]
        return None, None

    def set_active(self, session_id: str) -> None:
        """Set the active session."""
        if session_id in self.sessions:
            self.last_active = session_id
            self._db.execute(
                "INSERT OR REPLACE INTO state (key, value) VALUES ('last_active', ?)",
                (self.last_active,),
            )
            self._db.commit()

    def get_active(self) -> tuple[str | None, SessionInfo | None]:
        """Get the active session."""
        if self.last_active and self.last_active in self.sessions:
            return self.last_active, self.sessions[self.last_active]
        return None, None

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()
