"""Tests for the shared lock-reading utilities in :mod:`claude_xmpp_bridge.locks`."""

from __future__ import annotations

import json
from pathlib import Path

from claude_xmpp_bridge.locks import (
    _lock_dir,
    project_matches,
    read_legacy_lock_hints,
    short_path,
)

# ---------------------------------------------------------------------------
# short_path
# ---------------------------------------------------------------------------


def test_short_path_home():
    home = str(Path.home())
    assert short_path(home) == "~"


def test_short_path_subdir():
    home = str(Path.home())
    assert short_path(f"{home}/projects/foo") == "~/projects/foo"


def test_short_path_unrelated():
    assert short_path("/tmp/something") == "/tmp/something"


# ---------------------------------------------------------------------------
# project_matches
# ---------------------------------------------------------------------------


def test_project_matches_empty_filter():
    """An empty project filter matches everything."""
    assert project_matches("~/foo", "/home/x/foo/bar.py", "") is True


def test_project_matches_exact():
    home = str(Path.home())
    assert project_matches(f"{home}/foo", "/other", f"{home}/foo") is True


def test_project_matches_short():
    assert project_matches("~/foo", "/other", f"{Path.home()}/foo") is True


def test_project_matches_filepath_prefix():
    home = str(Path.home())
    assert project_matches("other-proj", f"{home}/foo/bar.py", f"{home}/foo") is True


def test_project_matches_no_match():
    assert project_matches("~/bar", "/tmp/baz", "/home/nobody/foo") is False


# ---------------------------------------------------------------------------
# read_legacy_lock_hints
# ---------------------------------------------------------------------------


def test_read_legacy_lock_hints_empty_dir(tmp_path, monkeypatch):
    """Returns empty list when lock dir doesn't exist."""
    monkeypatch.setattr("claude_xmpp_bridge.locks._lock_dir", lambda: tmp_path / "nonexistent")
    assert read_legacy_lock_hints() == []


def test_read_legacy_lock_hints_reads_files(tmp_path, monkeypatch):
    """Reads valid JSON lock files and returns structured entries."""
    monkeypatch.setattr("claude_xmpp_bridge.locks._lock_dir", lambda: tmp_path)
    lock_data = {
        "session_id": "ses_abc",
        "filepath": "/home/x/proj/file.py",
        "project": "~/proj",
        "locked_at": "2025-01-01T00:00:00",
    }
    (tmp_path / "lock1.json").write_text(json.dumps(lock_data))

    locks = read_legacy_lock_hints(active_session_ids={"ses_abc"})
    assert len(locks) == 1
    assert locks[0]["session_id"] == "ses_abc"
    assert locks[0]["stale"] is False
    assert locks[0]["source"] == "legacy"
    assert locks[0]["lockfile"] == str(tmp_path / "lock1.json")


def test_read_legacy_lock_hints_marks_stale(tmp_path, monkeypatch):
    """Locks for unregistered sessions are marked stale."""
    monkeypatch.setattr("claude_xmpp_bridge.locks._lock_dir", lambda: tmp_path)
    lock_data = {
        "session_id": "ses_gone",
        "filepath": "/home/x/proj/file.py",
        "project": "~/proj",
        "locked_at": "2025-01-01T00:00:00",
    }
    (tmp_path / "lock1.json").write_text(json.dumps(lock_data))

    locks = read_legacy_lock_hints(active_session_ids={"ses_other"})
    assert len(locks) == 1
    assert locks[0]["stale"] is True


def test_read_legacy_lock_hints_skips_invalid(tmp_path, monkeypatch):
    """Invalid JSON files and non-dict files are silently skipped."""
    monkeypatch.setattr("claude_xmpp_bridge.locks._lock_dir", lambda: tmp_path)
    (tmp_path / "bad.json").write_text("not json")
    (tmp_path / "array.json").write_text("[]")
    (tmp_path / "missing_fields.json").write_text(json.dumps({"session_id": "x"}))

    locks = read_legacy_lock_hints()
    assert locks == []


def test_read_legacy_lock_hints_project_filter(tmp_path, monkeypatch):
    """Project filter excludes non-matching locks."""
    monkeypatch.setattr("claude_xmpp_bridge.locks._lock_dir", lambda: tmp_path)
    lock_a = {"session_id": "a", "filepath": "/home/x/projA/f.py", "project": "~/projA", "locked_at": ""}
    lock_b = {"session_id": "b", "filepath": "/home/x/projB/f.py", "project": "~/projB", "locked_at": ""}
    (tmp_path / "a.json").write_text(json.dumps(lock_a))
    (tmp_path / "b.json").write_text(json.dumps(lock_b))

    home = str(Path.home())
    locks = read_legacy_lock_hints(project=f"{home}/projA")
    assert len(locks) == 1
    assert locks[0]["session_id"] == "a"


def test_lock_dir_returns_path():
    """_lock_dir() returns a Path pointing to ~/.claude/working."""
    result = _lock_dir()
    assert isinstance(result, Path)
    assert str(result).endswith(".claude/working")
