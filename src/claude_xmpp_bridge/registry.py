"""Session registry with SQLite persistence and input validation."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict

# Maximum number of queued inbox messages per session before the oldest are dropped.
MAX_INBOX_SIZE = 100

log = logging.getLogger(__name__)

# Validation patterns
SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
STY_RE = re.compile(
    r"^[a-zA-Z0-9_.%\-]{0,128}$"
)  # no colon: prevents tmux session:window injection; % allowed for tmux pane IDs
WINDOW_RE = re.compile(r"^[0-9]{0,6}$")  # max 6 digits, empty string allowed (screen default)


class SessionInfo(TypedDict):
    """Type for session data stored in the registry."""

    sty: str
    window: str
    project: str
    backend: str | None
    source: str | None  # "opencode", None = Claude Code
    registered_at: float
    plugin_version: str | None  # version string sent by plugin on register
    agent_state: str | None  # last known state: "idle", "running", etc.
    agent_mode: str | None  # last known mode: "planning", "code", "build"
    last_seen: float | None  # timestamp of last successful "state" heartbeat


class FileLockInfo(TypedDict):
    """Type for file-lock records stored in the registry."""

    session_id: str
    filepath: str
    project: str
    reason: str | None
    locked_at: str


def _validate_session_id(session_id: str) -> None:
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"Invalid session_id: {session_id!r} (must be 1-128 characters: letters, digits, underscore, hyphen)"
        )


def _validate_sty(sty: str) -> None:
    if not STY_RE.match(sty):
        raise ValueError(
            f"Invalid sty: {sty!r} (must contain only letters, digits, dots, percent, underscores, hyphens)"
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
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS inbox ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  to_session   TEXT    NOT NULL,"
            "  from_session TEXT,"
            "  message      TEXT    NOT NULL,"
            "  created_at   REAL    NOT NULL"
            ")"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS file_locks ("
            "  filepath   TEXT PRIMARY KEY,"
            "  session_id TEXT NOT NULL,"
            "  project    TEXT NOT NULL,"
            "  reason     TEXT,"
            "  locked_at  TEXT NOT NULL"
            ")"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS inbox_to_session ON inbox (to_session, id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS file_locks_session_id ON file_locks (session_id)")
        self._db.commit()
        # Migrations: add columns for existing databases that predate these fields
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        if "source" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
        if "plugin_version" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN plugin_version TEXT")
        if "agent_state" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN agent_state TEXT")
        if "agent_mode" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN agent_mode TEXT")
        if "last_seen" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN last_seen REAL")
        self._db.commit()
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load sessions and state from SQLite on startup."""
        for row in self._db.execute(
            "SELECT session_id, sty, window, project, backend, source, registered_at,"
            "       plugin_version, agent_state, agent_mode, last_seen FROM sessions"
        ):
            self.sessions[row[0]] = SessionInfo(
                sty=row[1] or "",
                window=row[2] or "",
                project=row[3] or "",
                backend=row[4],
                source=row[5],
                registered_at=float(row[6]),
                plugin_version=row[7],
                agent_state=row[8],
                agent_mode=row[9],
                last_seen=float(row[10]) if row[10] is not None else None,
            )
        row = self._db.execute("SELECT value FROM state WHERE key = 'last_active'").fetchone()
        if row and row[0] in self.sessions:
            self.last_active = row[0]
        log.info("Loaded %d sessions from DB", len(self.sessions))

    def _save_session(self, session_id: str) -> None:
        info = self.sessions[session_id]
        self._db.execute(
            "INSERT OR REPLACE INTO sessions"
            " (session_id, sty, window, project, backend, source,"
            "  registered_at, plugin_version, agent_state, agent_mode, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                info["sty"],
                info["window"],
                info["project"],
                info["backend"],
                info["source"],
                info["registered_at"],
                info.get("plugin_version"),
                info.get("agent_state"),
                info.get("agent_mode"),
                info.get("last_seen"),
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
        plugin_version: str | None = None,
    ) -> None:
        """Register a new session. Validates all inputs.

        registered_at: if provided, preserves the original slot timestamp so that
        the session keeps its position in /list after a re-registration.
        """
        _validate_session_id(session_id)
        _validate_sty(sty)
        _validate_window(window)

        is_reregister = session_id in self.sessions
        # Preserve agent_state, agent_mode and last_seen on re-register so a running agent doesn't lose its state.
        prev_state = self.sessions[session_id].get("agent_state") if is_reregister else None
        prev_mode = self.sessions[session_id].get("agent_mode") if is_reregister else None
        prev_last_seen = self.sessions[session_id].get("last_seen") if is_reregister else None
        self.sessions[session_id] = SessionInfo(
            sty=sty,
            window=window,
            project=project,
            backend=backend,
            source=source,
            registered_at=registered_at if registered_at is not None else time.time(),
            plugin_version=plugin_version,
            agent_state=prev_state,
            agent_mode=prev_mode,
            last_seen=prev_last_seen,
        )
        # Don't flip last_active when the same session re-registers — it would
        # hijack plain-text routing away from whatever the user targeted last.
        if not is_reregister:
            self.last_active = session_id
        self._save_session(session_id)
        log.info("Registered session %s (project=%s, backend=%s, source=%s)", session_id, project, backend, source)

    def unregister(self, session_id: str) -> None:
        """Unregister a session."""
        if session_id in self.sessions:
            released = self.release_all_file_locks(session_id)
            info = self.sessions.pop(session_id)
            log.info("Unregistered session %s (project=%s)", session_id, info["project"])
            if released:
                log.info("Released %d file lock(s) for session %s", released, session_id)
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
            self._save_last_active()
            self._db.commit()

    def update_state(self, session_id: str, state: str, mode: str | None = None) -> bool:
        """Update the agent_state (and optionally agent_mode) for a registered session.

        Also updates *last_seen* to the current time — the periodic ``state``
        call from the plugin serves as a liveness heartbeat.

        Returns True if the session exists and was updated, False otherwise.
        """
        info = self.sessions.get(session_id)
        if info is None:
            return False
        now = time.time()
        info["agent_state"] = state
        info["last_seen"] = now
        if mode is not None:
            info["agent_mode"] = mode
        self._db.execute(
            "UPDATE sessions SET agent_state = ?, agent_mode = ?, last_seen = ? WHERE session_id = ?",
            (state, info.get("agent_mode"), now, session_id),
        )
        self._db.commit()
        log.debug("State update: %s → %s (mode=%s)", session_id, state, mode)
        return True

    def get_active(self) -> tuple[str | None, SessionInfo | None]:
        """Get the active session."""
        if self.last_active and self.last_active in self.sessions:
            return self.last_active, self.sessions[self.last_active]
        return None, None

    def inbox_put(self, to_session: str, message: str, from_session: str | None = None) -> None:
        """Persistently enqueue a message in the inbox for *to_session*.

        If the inbox already contains MAX_INBOX_SIZE messages for this session,
        the oldest one is dropped to make room (same policy as the previous
        in-memory asyncio.Queue with maxsize=100).
        """
        with self._db:
            count: int = self._db.execute("SELECT COUNT(*) FROM inbox WHERE to_session = ?", (to_session,)).fetchone()[
                0
            ]
            if count >= MAX_INBOX_SIZE:
                # Drop the oldest message for this session.
                self._db.execute(
                    "DELETE FROM inbox WHERE to_session = ? AND id = (SELECT MIN(id) FROM inbox WHERE to_session = ?)",
                    (to_session, to_session),
                )
            self._db.execute(
                "INSERT INTO inbox (to_session, from_session, message, created_at) VALUES (?, ?, ?, ?)",
                (to_session, from_session, message, time.time()),
            )

    def inbox_drain(self, session_id: str) -> list[str]:
        """Atomically drain and return all pending messages for *session_id*.

        Messages are returned in insertion order (oldest first).  The rows are
        deleted in the same transaction so concurrent callers cannot receive
        the same message twice.
        """
        with self._db:
            rows = self._db.execute(
                "SELECT id, message FROM inbox WHERE to_session = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                self._db.execute(
                    f"DELETE FROM inbox WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
                    ids,
                )
        return [r[1] for r in rows]

    def inbox_count(self, session_id: str) -> int:
        """Return the number of pending messages for *session_id*."""
        return int(self._db.execute("SELECT COUNT(*) FROM inbox WHERE to_session = ?", (session_id,)).fetchone()[0])

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _row_to_file_lock(row: sqlite3.Row | tuple[object, ...]) -> FileLockInfo:
        return FileLockInfo(
            filepath=str(row[0]),
            session_id=str(row[1]),
            project=str(row[2]),
            reason=str(row[3]) if row[3] is not None else None,
            locked_at=str(row[4]),
        )

    def list_file_locks(self) -> list[FileLockInfo]:
        """Return all bridge-native file locks sorted by timestamp/path."""
        rows = self._db.execute(
            "SELECT filepath, session_id, project, reason, locked_at FROM file_locks ORDER BY locked_at, filepath"
        ).fetchall()
        return [self._row_to_file_lock(row) for row in rows]

    def acquire_file_lock(
        self, session_id: str, filepath: str, project: str, reason: str | None = None
    ) -> tuple[bool, FileLockInfo, bool]:
        """Acquire a bridge-native file lock.

        Returns ``(acquired, lock, replaced_stale)``. If another active session
        owns the lock, ``acquired`` is False and ``lock`` describes the current owner.
        If the previous owner session is no longer registered, the stale lock is
        replaced automatically and ``replaced_stale`` is True.
        """
        now = self._now_iso()
        with self._db:
            row = self._db.execute(
                "SELECT filepath, session_id, project, reason, locked_at FROM file_locks WHERE filepath = ?",
                (filepath,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO file_locks (filepath, session_id, project, reason, locked_at) VALUES (?, ?, ?, ?, ?)",
                    (filepath, session_id, project, reason, now),
                )
                return True, FileLockInfo(
                    filepath=filepath,
                    session_id=session_id,
                    project=project,
                    reason=reason,
                    locked_at=now,
                ), False

            existing = self._row_to_file_lock(row)
            if existing["session_id"] == session_id or existing["session_id"] not in self.sessions:
                self._db.execute(
                    "UPDATE file_locks SET session_id = ?, project = ?, reason = ?, locked_at = ? WHERE filepath = ?",
                    (session_id, project, reason, now, filepath),
                )
                return True, FileLockInfo(
                    filepath=filepath,
                    session_id=session_id,
                    project=project,
                    reason=reason,
                    locked_at=now,
                ), existing["session_id"] != session_id

            return False, existing, False

    def release_file_lock(self, session_id: str, filepath: str, force: bool = False) -> bool:
        """Release a bridge-native file lock by owner session.

        If ``force`` is True, removes the lock regardless of owner.
        Returns True if a row was deleted.
        """
        with self._db:
            if force:
                cur = self._db.execute("DELETE FROM file_locks WHERE filepath = ?", (filepath,))
            else:
                cur = self._db.execute(
                    "DELETE FROM file_locks WHERE filepath = ? AND session_id = ?",
                    (filepath, session_id),
                )
        return cur.rowcount > 0

    def release_all_file_locks(self, session_id: str) -> int:
        """Release all bridge-native file locks held by *session_id*."""
        with self._db:
            cur = self._db.execute("DELETE FROM file_locks WHERE session_id = ?", (session_id,))
        return int(cur.rowcount)

    def cleanup_stale_file_locks(self) -> list[FileLockInfo]:
        """Remove file locks whose owner session is no longer registered."""
        stale = [lock for lock in self.list_file_locks() if lock["session_id"] not in self.sessions]
        if not stale:
            return []
        with self._db:
            self._db.executemany("DELETE FROM file_locks WHERE filepath = ?", [(lock["filepath"],) for lock in stale])
        return stale

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()
