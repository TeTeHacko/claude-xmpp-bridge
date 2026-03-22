"""Session registry with SQLite persistence and input validation."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
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
    todos_version: int  # optimistic-lock version for bridge-native todos
    last_agent_sender: str | None  # last known agent session_id that sent a relay/inbox message


class FileLockInfo(TypedDict):
    """Type for file-lock records stored in the registry."""

    session_id: str
    filepath: str
    project: str
    reason: str | None
    locked_at: str


class InboxMessage(TypedDict):
    """Type for inbox message rows returned by ``inbox_drain_full()``."""

    message: str
    from_session: str | None
    source_type: str | None
    message_type: str | None
    from_label: str | None
    created_at: float


class DelegatedTask(TypedDict):
    """Type for delegated task records stored in the registry."""

    task_id: str
    from_session: str
    to_session: str
    description: str
    context: str | None
    status: str  # pending, accepted, completed, failed, cancelled
    result: str | None
    created_at: float
    updated_at: float


class TodoInfo(TypedDict):
    """Type for per-session todo items stored in the registry."""

    todo_id: str
    content: str
    status: str
    priority: str
    updated_at: str


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
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS todos ("
            "  session_id  TEXT NOT NULL,"
            "  position    INTEGER NOT NULL,"
            "  todo_id     TEXT NOT NULL,"
            "  content     TEXT NOT NULL,"
            "  status      TEXT NOT NULL,"
            "  priority    TEXT NOT NULL,"
            "  updated_at  TEXT NOT NULL,"
            "  PRIMARY KEY (session_id, position)"
            ")"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS inbox_to_session ON inbox (to_session, id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS file_locks_session_id ON file_locks (session_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS todos_session_id ON todos (session_id, position)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS delegated_tasks ("
            "  task_id     TEXT PRIMARY KEY,"
            "  from_session TEXT NOT NULL,"
            "  to_session   TEXT NOT NULL,"
            "  description  TEXT NOT NULL,"
            "  context      TEXT,"
            "  status       TEXT NOT NULL DEFAULT 'pending',"
            "  result       TEXT,"
            "  created_at   REAL NOT NULL,"
            "  updated_at   REAL NOT NULL"
            ")"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS delegated_tasks_from ON delegated_tasks (from_session, status)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS delegated_tasks_to ON delegated_tasks (to_session, status)"
        )
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
        if "todos_version" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN todos_version INTEGER NOT NULL DEFAULT 0")
        if "last_agent_sender" not in cols:
            self._db.execute("ALTER TABLE sessions ADD COLUMN last_agent_sender TEXT")
        inbox_cols = {row[1] for row in self._db.execute("PRAGMA table_info(inbox)")}
        if "source_type" not in inbox_cols:
            self._db.execute("ALTER TABLE inbox ADD COLUMN source_type TEXT")
        if "message_type" not in inbox_cols:
            self._db.execute("ALTER TABLE inbox ADD COLUMN message_type TEXT")
        if "from_label" not in inbox_cols:
            self._db.execute("ALTER TABLE inbox ADD COLUMN from_label TEXT")
        todo_cols = {row[1] for row in self._db.execute("PRAGMA table_info(todos)")}
        if todo_cols and "todo_id" not in todo_cols:
            self._db.execute("ALTER TABLE todos ADD COLUMN todo_id TEXT")
            rows = self._db.execute("SELECT session_id, position FROM todos").fetchall()
            self._db.executemany(
                "UPDATE todos SET todo_id = ? WHERE session_id = ? AND position = ?",
                [(uuid.uuid4().hex[:12], row[0], row[1]) for row in rows],
            )
        self._db.commit()
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load sessions and state from SQLite on startup."""
        for row in self._db.execute(
            "SELECT session_id, sty, window, project, backend, source, registered_at,"
            "       plugin_version, agent_state, agent_mode, last_seen, todos_version, last_agent_sender FROM sessions"
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
                todos_version=int(row[11] or 0),
                last_agent_sender=row[12],
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
            "  registered_at, plugin_version, agent_state, agent_mode, last_seen, todos_version, last_agent_sender)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                info.get("todos_version", 0),
                info.get("last_agent_sender"),
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
        prev_todos_version = self.sessions[session_id].get("todos_version", 0) if is_reregister else 0
        prev_last_agent_sender = self.sessions[session_id].get("last_agent_sender") if is_reregister else None
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
            todos_version=prev_todos_version,
            last_agent_sender=prev_last_agent_sender,
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
            self.clear_todos(session_id)
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

    def inbox_put(
        self,
        to_session: str,
        message: str,
        from_session: str | None = None,
        *,
        source_type: str | None = None,
        message_type: str | None = None,
        from_label: str | None = None,
    ) -> None:
        """Persistently enqueue a message in the inbox for *to_session*.

        If the inbox already contains MAX_INBOX_SIZE messages for this session,
        the oldest one is dropped to make room (same policy as the previous
        in-memory asyncio.Queue with maxsize=100).

        Args:
            source_type: Origin of the message — ``"agent"``, ``"human"``, or
                ``"system"``.  Defaults to ``None`` (legacy callers).
            message_type: Semantic type — ``"relay"``, ``"broadcast"``,
                ``"task_request"``, ``"task_result"``, etc.
            from_label: Human-readable sender label, e.g. ``"w1"`` or ``"w3"``.
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
                "INSERT INTO inbox (to_session, from_session, message,"
                " created_at, source_type, message_type, from_label)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (to_session, from_session, message, time.time(), source_type, message_type, from_label),
            )

    def inbox_drain_full(self, session_id: str) -> list[InboxMessage]:
        """Atomically drain messages with full metadata.

        Returns a list of :class:`InboxMessage` dicts containing all stored
        columns: ``message``, ``from_session``, ``source_type``,
        ``message_type``, ``from_label``, and ``created_at``.
        """
        with self._db:
            rows = self._db.execute(
                "SELECT id, message, from_session, source_type, message_type, created_at, from_label"
                " FROM inbox WHERE to_session = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                self._db.execute(
                    f"DELETE FROM inbox WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
                    ids,
                )
        return [
            InboxMessage(
                message=r[1],
                from_session=r[2],
                source_type=r[3],
                message_type=r[4],
                from_label=r[6],
                created_at=float(r[5]),
            )
            for r in rows
        ]

    def set_last_agent_sender(self, session_id: str, sender_session_id: str | None) -> bool:
        """Persist the last replyable agent sender for a session."""
        if session_id not in self.sessions:
            return False
        self.sessions[session_id]["last_agent_sender"] = sender_session_id
        self._db.execute(
            "UPDATE sessions SET last_agent_sender = ? WHERE session_id = ?",
            (sender_session_id, session_id),
        )
        self._db.commit()
        return True

    def get_last_agent_sender(self, session_id: str) -> str | None:
        """Return the last remembered replyable agent sender for a session."""
        info = self.sessions.get(session_id)
        if not info:
            return None
        return info.get("last_agent_sender")

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

    def list_file_locks_for_session(self, session_id: str) -> list[FileLockInfo]:
        """Return bridge-native file locks owned by *session_id*."""
        rows = self._db.execute(
            "SELECT filepath, session_id, project, reason, locked_at"
            " FROM file_locks WHERE session_id = ? ORDER BY locked_at, filepath",
            (session_id,),
        ).fetchall()
        return [self._row_to_file_lock(row) for row in rows]

    def file_lock_count(self, session_id: str) -> int:
        """Return the number of bridge-native file locks held by *session_id*."""
        row = self._db.execute("SELECT COUNT(*) FROM file_locks WHERE session_id = ?", (session_id,)).fetchone()
        return int(row[0])

    @staticmethod
    def _normalize_filepath(filepath: str) -> str:
        """Normalize a file path for lock identity purposes."""
        return str(Path(filepath).expanduser().resolve(strict=False))

    def acquire_file_lock(
        self, session_id: str, filepath: str, project: str, reason: str | None = None
    ) -> tuple[bool, FileLockInfo, bool]:
        """Acquire a bridge-native file lock.

        Returns ``(acquired, lock, replaced_stale)``. If another active session
        owns the lock, ``acquired`` is False and ``lock`` describes the current owner.
        If the previous owner session is no longer registered, the stale lock is
        replaced automatically and ``replaced_stale`` is True.
        """
        filepath = self._normalize_filepath(filepath)
        now = self._now_iso()
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO file_locks"
                " (filepath, session_id, project, reason, locked_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (filepath, session_id, project, reason, now),
            )
            if cur.rowcount == 1:
                return True, FileLockInfo(
                    filepath=filepath,
                    session_id=session_id,
                    project=project,
                    reason=reason,
                    locked_at=now,
                ), False

            row = self._db.execute(
                "SELECT filepath, session_id, project, reason, locked_at FROM file_locks WHERE filepath = ?",
                (filepath,),
            ).fetchone()
            if row is None:  # pragma: no cover - defensive; INSERT OR IGNORE should have created/found a row
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
        filepath = self._normalize_filepath(filepath)
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

    def cleanup_stale_file_locks(self, project: str | None = None) -> list[FileLockInfo]:
        """Remove file locks whose owner session is no longer registered."""
        stale = [lock for lock in self.list_file_locks() if lock["session_id"] not in self.sessions]
        if project:
            stale = [lock for lock in stale if lock["project"] == project]
        if not stale:
            return []
        with self._db:
            self._db.executemany("DELETE FROM file_locks WHERE filepath = ?", [(lock["filepath"],) for lock in stale])
        return stale

    @staticmethod
    def _row_to_todo(row: sqlite3.Row | tuple[object, ...]) -> TodoInfo:
        return TodoInfo(
            todo_id=str(row[0]),
            content=str(row[1]),
            status=str(row[2]),
            priority=str(row[3]),
            updated_at=str(row[4]),
        )

    def replace_todos(
        self,
        session_id: str,
        todos: Sequence[TodoInfo | Mapping[str, object]],
        expected_version: int | None = None,
    ) -> int | None:
        """Replace the todo list for *session_id* atomically.

        Returns the new todo version on success, or None if ``expected_version``
        was provided and did not match the current version.
        """
        now = self._now_iso()
        rows: list[tuple[str, int, str, str, str, str, str]] = []
        for idx, todo in enumerate(todos, start=1):
            rows.append(
                (
                    session_id,
                    idx,
                    str(todo.get("todo_id", "")).strip() or uuid.uuid4().hex[:12],
                    str(todo.get("content", "")).strip(),
                    str(todo.get("status", "pending")).strip() or "pending",
                    str(todo.get("priority", "medium")).strip() or "medium",
                    now,
                )
            )
        info = self.sessions.get(session_id)
        if info is None:
            return None
        current_version = int(info.get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return None

        new_version = current_version + 1
        with self._db:
            self._db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
            if rows:
                self._db.executemany(
                    "INSERT INTO todos"
                    " (session_id, position, todo_id, content, status, priority, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            self._db.execute("UPDATE sessions SET todos_version = ? WHERE session_id = ?", (new_version, session_id))
        info["todos_version"] = new_version
        return new_version

    def list_todos(self, session_id: str) -> list[TodoInfo]:
        """Return the todo list for *session_id* in order."""
        rows = self._db.execute(
            "SELECT todo_id, content, status, priority, updated_at FROM todos WHERE session_id = ? ORDER BY position",
            (session_id,),
        ).fetchall()
        return [self._row_to_todo(row) for row in rows]

    def todo_count(self, session_id: str) -> int:
        """Return the number of todos stored for *session_id*."""
        return int(self._db.execute("SELECT COUNT(*) FROM todos WHERE session_id = ?", (session_id,)).fetchone()[0])

    def clear_todos(self, session_id: str) -> int:
        """Delete all todos for *session_id* and return the number removed."""
        with self._db:
            cur = self._db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
            self._db.execute(
                "UPDATE sessions SET todos_version = COALESCE(todos_version, 0) + 1 WHERE session_id = ?",
                (session_id,),
            )
        if session_id in self.sessions:
            self.sessions[session_id]["todos_version"] = int(self.sessions[session_id].get("todos_version", 0)) + 1
        return int(cur.rowcount)

    def add_todo(
        self,
        session_id: str,
        content: str,
        status: str = "pending",
        priority: str = "medium",
        expected_version: int | None = None,
    ) -> tuple[TodoInfo | None, int | None]:
        """Append a todo to the end of the session list and return it with new version."""
        info = self.sessions.get(session_id)
        if info is None:
            return None, None
        current_version = int(info.get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return None, None
        todo_id = uuid.uuid4().hex[:12]
        updated_at = self._now_iso()
        position_row = self._db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM todos WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        position = int(position_row[0])
        new_version = current_version + 1
        todo = TodoInfo(
            todo_id=todo_id,
            content=content.strip(),
            status=status.strip() or "pending",
            priority=priority.strip() or "medium",
            updated_at=updated_at,
        )
        with self._db:
            self._db.execute(
                "INSERT INTO todos"
                " (session_id, position, todo_id, content, status, priority, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, position, todo_id, todo["content"], todo["status"], todo["priority"], updated_at),
            )
            self._db.execute("UPDATE sessions SET todos_version = ? WHERE session_id = ?", (new_version, session_id))
        info["todos_version"] = new_version
        return todo, new_version

    def update_todo(
        self,
        session_id: str,
        todo_id: str,
        *,
        content: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[TodoInfo | None, int | None]:
        """Update one todo item by id and return it with the new version."""
        info = self.sessions.get(session_id)
        if info is None:
            return None, None
        current_version = int(info.get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return None, None
        row = self._db.execute(
            "SELECT todo_id, content, status, priority, updated_at FROM todos WHERE session_id = ? AND todo_id = ?",
            (session_id, todo_id),
        ).fetchone()
        if row is None:
            return None, None
        current = self._row_to_todo(row)
        updated = TodoInfo(
            todo_id=current["todo_id"],
            content=content.strip() if content is not None else current["content"],
            status=status.strip() if status is not None else current["status"],
            priority=priority.strip() if priority is not None else current["priority"],
            updated_at=self._now_iso(),
        )
        new_version = current_version + 1
        with self._db:
            self._db.execute(
                "UPDATE todos SET content = ?, status = ?, priority = ?, updated_at = ?"
                " WHERE session_id = ? AND todo_id = ?",
                (
                    updated["content"],
                    updated["status"],
                    updated["priority"],
                    updated["updated_at"],
                    session_id,
                    todo_id,
                ),
            )
            self._db.execute("UPDATE sessions SET todos_version = ? WHERE session_id = ?", (new_version, session_id))
        info["todos_version"] = new_version
        return updated, new_version

    def remove_todo(
        self, session_id: str, todo_id: str, expected_version: int | None = None
    ) -> tuple[bool, int | None]:
        """Remove one todo item by id and compact positions."""
        info = self.sessions.get(session_id)
        if info is None:
            return False, None
        current_version = int(info.get("todos_version", 0))
        if expected_version is not None and expected_version != current_version:
            return False, None
        row = self._db.execute(
            "SELECT position FROM todos WHERE session_id = ? AND todo_id = ?",
            (session_id, todo_id),
        ).fetchone()
        if row is None:
            return False, None
        position = int(row[0])
        new_version = current_version + 1
        with self._db:
            self._db.execute("DELETE FROM todos WHERE session_id = ? AND todo_id = ?", (session_id, todo_id))
            self._db.execute(
                "UPDATE todos SET position = position - 1 WHERE session_id = ? AND position > ?",
                (session_id, position),
            )
            self._db.execute("UPDATE sessions SET todos_version = ? WHERE session_id = ?", (new_version, session_id))
        info["todos_version"] = new_version
        return True, new_version

    def task_create(
        self,
        *,
        task_id: str,
        from_session: str,
        to_session: str,
        description: str,
        context: str | None = None,
    ) -> DelegatedTask:
        """Create a new delegated task record.

        Returns the created :class:`DelegatedTask` dict.
        """
        now = time.time()
        task = DelegatedTask(
            task_id=task_id,
            from_session=from_session,
            to_session=to_session,
            description=description,
            context=context,
            status="pending",
            result=None,
            created_at=now,
            updated_at=now,
        )
        with self._db:
            self._db.execute(
                "INSERT INTO delegated_tasks"
                " (task_id, from_session, to_session, description, context, status, result, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, from_session, to_session, description, context, "pending", None, now, now),
            )
        return task

    # Valid state transitions for delegated tasks.
    # ``cancelled`` is reachable from any non-terminal state.
    _TASK_TRANSITIONS: dict[str, set[str]] = {
        "pending": {"accepted", "completed", "failed", "cancelled"},
        "accepted": {"completed", "failed", "cancelled"},
        # Terminal states — no further transitions allowed.
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }

    def task_update_status(
        self,
        task_id: str,
        status: str,
        result: str | None = None,
    ) -> DelegatedTask | None:
        """Update the status (and optionally the result) of a delegated task.

        Valid status transitions: pending → accepted/completed/failed/cancelled,
        accepted → completed/failed/cancelled.  Terminal states (completed,
        failed, cancelled) do not allow further transitions.

        Returns the updated task, or None if *task_id* is not found.
        Raises ``ValueError`` if the transition is invalid.
        """
        now = time.time()
        with self._db:
            row = self._db.execute(
                "SELECT task_id, from_session, to_session, description, context,"
                "       status, result, created_at, updated_at"
                " FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            current_status = str(row[5])
            allowed = self._TASK_TRANSITIONS.get(current_status, set())
            if status not in allowed:
                msg = f"invalid task transition: {current_status!r} → {status!r}"
                raise ValueError(msg)
            self._db.execute(
                "UPDATE delegated_tasks SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
                (status, result, now, task_id),
            )
        return DelegatedTask(
            task_id=row[0],
            from_session=row[1],
            to_session=row[2],
            description=row[3],
            context=row[4],
            status=status,
            result=result if result is not None else row[6],
            created_at=float(row[7]),
            updated_at=now,
        )

    def task_get(self, task_id: str) -> DelegatedTask | None:
        """Return a single delegated task by ID, or None if not found."""
        row = self._db.execute(
            "SELECT task_id, from_session, to_session, description, context,"
            "       status, result, created_at, updated_at"
            " FROM delegated_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def task_list(
        self,
        *,
        session_id: str | None = None,
        role: str = "both",
        status: str | None = None,
    ) -> list[DelegatedTask]:
        """List delegated tasks, optionally filtered.

        Args:
            session_id: If set, only tasks involving this session.
            role: ``"from"`` (delegator), ``"to"`` (assignee), or ``"both"``.
            status: If set, only tasks with this status.
        """
        clauses: list[str] = []
        params: list[str] = []
        if session_id:
            if role == "from":
                clauses.append("from_session = ?")
                params.append(session_id)
            elif role == "to":
                clauses.append("to_session = ?")
                params.append(session_id)
            else:
                clauses.append("(from_session = ? OR to_session = ?)")
                params.extend([session_id, session_id])
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (  # noqa: S608 – clauses are hardcoded strings, not user input
            "SELECT task_id, from_session, to_session, description, context,"
            " status, result, created_at, updated_at FROM delegated_tasks"
        ) + where + " ORDER BY created_at DESC"
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    @staticmethod
    def _row_to_task(row: sqlite3.Row | tuple[object, ...]) -> DelegatedTask:
        return DelegatedTask(
            task_id=str(row[0]),
            from_session=str(row[1]),
            to_session=str(row[2]),
            description=str(row[3]),
            context=str(row[4]) if row[4] is not None else None,
            status=str(row[5]),
            result=str(row[6]) if row[6] is not None else None,
            created_at=float(str(row[7])),
            updated_at=float(str(row[8])),
        )

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()
