"""Shared utilities for reading legacy file-lock hints from ``~/.claude/working``.

Both :mod:`bridge` and :mod:`mcp_server` need to iterate this directory.
Extracting the logic here avoids duplication and keeps the two modules in sync.
"""

from __future__ import annotations

import json
from pathlib import Path


def _lock_dir() -> Path:
    """Return the legacy lock-hint directory (evaluated at call time)."""
    return Path.home() / ".claude" / "working"


def short_path(path: str) -> str:
    """Replace ``$HOME`` prefix with ``~`` for display."""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def project_matches(lock_project: str, lock_filepath: str, project: str) -> bool:
    """Return ``True`` if a lock entry belongs to the requested *project* filter.

    An empty *project* string means "match everything".
    """
    if not project:
        return True
    short = short_path(project)
    return lock_project in {project, short} or lock_filepath.startswith(project)


def read_legacy_lock_hints(
    *,
    project: str = "",
    active_session_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Read all legacy lock hint files from ``~/.claude/working``.

    Each file is expected to contain a JSON object with ``session_id``,
    ``filepath``, ``project``, and ``locked_at`` keys.

    Parameters
    ----------
    project:
        Optional project filter — only locks matching this project are returned.
    active_session_ids:
        Set of currently registered session IDs.  Used to set the ``stale``
        flag on each lock entry.  If ``None``, all locks are marked non-stale.
    """
    lock_dir = _lock_dir()
    if not lock_dir.is_dir():
        return []

    if active_session_ids is None:
        active_session_ids = set()

    locks: list[dict[str, object]] = []
    for path in sorted(lock_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("session_id", "")).strip()
        filepath = str(data.get("filepath", "")).strip()
        lock_project = str(data.get("project", "")).strip()
        locked_at = str(data.get("locked_at", "")).strip()
        if not session_id or not filepath:
            continue
        if not project_matches(lock_project, filepath, project):
            continue
        locks.append(
            {
                "session_id": session_id,
                "filepath": filepath,
                "project": lock_project,
                "locked_at": locked_at,
                "stale": session_id not in active_session_ids,
                "source": "legacy",
                "lockfile": str(path),
            }
        )
    return locks
