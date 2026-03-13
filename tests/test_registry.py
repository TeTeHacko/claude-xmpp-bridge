"""Tests for SessionRegistry — lifecycle, persistence, ordering, validation."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from claude_xmpp_bridge.registry import SessionRegistry

# ---------------------------------------------------------------------------
# 1. Register / unregister lifecycle
# ---------------------------------------------------------------------------


def test_register_and_list(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("sess-1", "12345.pts-0.host", "0", "/home/user/project")
        sessions = reg.list_sessions()
        assert "sess-1" in sessions
        assert sessions["sess-1"]["sty"] == "12345.pts-0.host"
        assert sessions["sess-1"]["window"] == "0"
        assert sessions["sess-1"]["project"] == "/home/user/project"
    finally:
        reg.close()


def test_unregister_removes_session(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("sess-1", "12345.pts-0.host", "0", "/tmp/proj")
        reg.unregister("sess-1")
        assert "sess-1" not in reg.list_sessions()
        assert reg.get("sess-1") is None
    finally:
        reg.close()


def test_unregister_nonexistent_is_noop(db_path):
    """Unregistering a session that was never registered should not raise."""
    reg = SessionRegistry(db_path)
    try:
        reg.unregister("no-such-session")  # must not raise
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 2. Persistence — survive registry restart
# ---------------------------------------------------------------------------


def test_persistence_sessions_loaded(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("alpha", "100.pts-0.box", "1", "/proj/a", backend="screen")
        reg1.register("beta", "200.pts-1.box", "2", "/proj/b")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        sessions = reg2.list_sessions()
        assert "alpha" in sessions
        assert "beta" in sessions
        assert sessions["alpha"]["backend"] == "screen"
        assert sessions["beta"]["project"] == "/proj/b"
    finally:
        reg2.close()


def test_persistence_last_active_loaded(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("first", "", "", "/proj/1")
        reg1.register("second", "", "", "/proj/2")
        reg1.set_active("first")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        active_id, active_info = reg2.get_active()
        assert active_id == "first"
        assert active_info["project"] == "/proj/1"
    finally:
        reg2.close()


def test_persistence_unregister_reflected(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("keep", "", "", "/proj/keep")
        reg1.register("drop", "", "", "/proj/drop")
        reg1.unregister("drop")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        assert "keep" in reg2.list_sessions()
        assert "drop" not in reg2.list_sessions()
    finally:
        reg2.close()


# ---------------------------------------------------------------------------
# 3. get_by_index — 1-based, sorted by registration time
# ---------------------------------------------------------------------------


def test_get_by_index_order(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("aaa", "", "", "/proj/a")
        time.sleep(0.01)
        reg.register("bbb", "", "", "/proj/b")
        time.sleep(0.01)
        reg.register("ccc", "", "", "/proj/c")

        sid1, _ = reg.get_by_index(1)
        sid2, _ = reg.get_by_index(2)
        sid3, _ = reg.get_by_index(3)
        assert sid1 == "aaa"
        assert sid2 == "bbb"
        assert sid3 == "ccc"
    finally:
        reg.close()


def test_get_by_index_out_of_range(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("only", "", "", "/proj")
        assert reg.get_by_index(0) == (None, None)
        assert reg.get_by_index(2) == (None, None)
        assert reg.get_by_index(-1) == (None, None)
    finally:
        reg.close()


def test_get_by_index_empty(db_path):
    reg = SessionRegistry(db_path)
    try:
        assert reg.get_by_index(1) == (None, None)
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 4. set_active / get_active
# ---------------------------------------------------------------------------


def test_set_active_and_get_active(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        reg.register("s2", "", "", "/proj/2")
        # last registered becomes active
        assert reg.get_active()[0] == "s2"

        reg.set_active("s1")
        assert reg.get_active()[0] == "s1"
    finally:
        reg.close()


def test_set_active_unknown_session_is_noop(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("real", "", "", "/proj")
        reg.set_active("nonexistent")
        # active should still be "real" (last registered)
        assert reg.get_active()[0] == "real"
    finally:
        reg.close()


def test_get_active_empty_registry(db_path):
    reg = SessionRegistry(db_path)
    try:
        assert reg.get_active() == (None, None)
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 5b. bridge-native file locks
# ---------------------------------------------------------------------------


def test_acquire_file_lock_succeeds_for_new_file(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        acquired, lock, replaced_stale = reg.acquire_file_lock("s1", "/tmp/a.py", "/proj/1", "edit")
        assert acquired is True
        assert replaced_stale is False
        assert lock["session_id"] == "s1"
        assert lock["filepath"] == "/tmp/a.py"
        assert lock["reason"] == "edit"
    finally:
        reg.close()


def test_acquire_file_lock_rejects_other_active_session(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        reg.register("s2", "", "", "/proj/2")
        reg.acquire_file_lock("s1", "/tmp/a.py", "/proj/1")
        acquired, lock, replaced_stale = reg.acquire_file_lock("s2", "/tmp/a.py", "/proj/2")
        assert acquired is False
        assert replaced_stale is False
        assert lock["session_id"] == "s1"
    finally:
        reg.close()


def test_acquire_file_lock_replaces_stale_owner(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg._db.execute(
            "INSERT INTO file_locks (filepath, session_id, project, reason, locked_at) VALUES (?, ?, ?, ?, ?)",
            ("/tmp/a.py", "s1", "/proj/1", None, "2026-03-11T01:00:00+01:00"),
        )
        reg._db.commit()
        reg.register("s2", "", "", "/proj/2")
        acquired, lock, replaced_stale = reg.acquire_file_lock("s2", "/tmp/a.py", "/proj/2")
        assert acquired is True
        assert replaced_stale is True
        assert lock["session_id"] == "s2"
    finally:
        reg.close()


def test_release_all_file_locks_on_unregister(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        reg.acquire_file_lock("s1", "/tmp/a.py", "/proj/1")
        reg.acquire_file_lock("s1", "/tmp/b.py", "/proj/1")
        reg.unregister("s1")
        assert reg.list_file_locks() == []
    finally:
        reg.close()


def test_cleanup_stale_file_locks_removes_only_stale(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("live", "", "", "/proj/live")
        reg.acquire_file_lock("live", "/tmp/live.py", "/proj/live")
        reg._db.execute(
            "INSERT INTO file_locks (filepath, session_id, project, reason, locked_at) VALUES (?, ?, ?, ?, ?)",
            ("/tmp/stale.py", "stale", "/proj/stale", None, "2026-03-11T01:00:00+01:00"),
        )
        reg._db.commit()
        removed = reg.cleanup_stale_file_locks()
        assert len(removed) == 1
        assert removed[0]["filepath"] == "/tmp/stale.py"
        assert [lock["filepath"] for lock in reg.list_file_locks()] == ["/tmp/live.py"]
    finally:
        reg.close()


def test_cleanup_stale_file_locks_respects_project_filter(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg._db.execute(
            "INSERT INTO file_locks (filepath, session_id, project, reason, locked_at) VALUES (?, ?, ?, ?, ?)",
            ("/tmp/stale-a.py", "stale-a", "/proj/a", None, "2026-03-11T01:00:00+01:00"),
        )
        reg._db.execute(
            "INSERT INTO file_locks (filepath, session_id, project, reason, locked_at) VALUES (?, ?, ?, ?, ?)",
            ("/tmp/stale-b.py", "stale-b", "/proj/b", None, "2026-03-11T01:00:01+01:00"),
        )
        reg._db.commit()
        removed = reg.cleanup_stale_file_locks(project="/proj/a")
        assert [lock["filepath"] for lock in removed] == ["/tmp/stale-a.py"]
        assert [lock["filepath"] for lock in reg.list_file_locks()] == ["/tmp/stale-b.py"]
    finally:
        reg.close()


def test_file_lock_normalizes_equivalent_paths(db_path, tmp_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", str(tmp_path))
        reg.register("s2", "", "", str(tmp_path))
        file_path = tmp_path / "dir" / "a.py"
        rel_path = tmp_path / "dir" / "." / "a.py"
        acquired, lock, _ = reg.acquire_file_lock("s1", str(file_path), str(tmp_path))
        assert acquired is True
        acquired2, lock2, _ = reg.acquire_file_lock("s2", str(rel_path), str(tmp_path))
        assert acquired2 is False
        assert lock2["session_id"] == "s1"
        assert lock["filepath"] == str(Path(file_path).resolve(strict=False))
    finally:
        reg.close()


def test_replace_and_list_todos(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        reg.replace_todos(
            "s1",
            [
                {"content": "first", "status": "pending", "priority": "high"},
                {"content": "second", "status": "completed", "priority": "low"},
            ],
        )
        todos = reg.list_todos("s1")
        assert [todo["content"] for todo in todos] == ["first", "second"]
        assert todos[0]["status"] == "pending"
        assert todos[1]["priority"] == "low"
        assert reg.get("s1")["todos_version"] == 1
    finally:
        reg.close()


def test_replace_todos_overwrites_previous_list(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        reg.replace_todos("s1", [{"content": "first", "status": "pending", "priority": "high"}])
        reg.replace_todos("s1", [{"content": "only", "status": "in_progress", "priority": "medium"}])
        todos = reg.list_todos("s1")
        assert len(todos) == 1
        assert todos[0]["content"] == "only"
        assert reg.todo_count("s1") == 1
        assert reg.get("s1")["todos_version"] == 2
    finally:
        reg.close()


def test_replace_todos_expected_version_conflict(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        version1 = reg.replace_todos("s1", [{"content": "first", "status": "pending", "priority": "high"}])
        assert version1 == 1
        version2 = reg.replace_todos(
            "s1",
            [{"content": "second", "status": "pending", "priority": "high"}],
            expected_version=0,
        )
        assert version2 is None
        assert [todo["content"] for todo in reg.list_todos("s1")] == ["first"]
    finally:
        reg.close()


def test_unregister_clears_todos(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        reg.replace_todos("s1", [{"content": "first", "status": "pending", "priority": "high"}])
        reg.unregister("s1")
        assert reg.list_todos("s1") == []
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 5. last_active fallback — unregister active → most recent remaining
# ---------------------------------------------------------------------------


def test_fallback_to_most_recent_remaining(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("old", "", "", "/proj/old")
        time.sleep(0.01)
        reg.register("mid", "", "", "/proj/mid")
        time.sleep(0.01)
        reg.register("new", "", "", "/proj/new")
        # "new" is active
        assert reg.get_active()[0] == "new"

        reg.unregister("new")
        # should fall back to "mid" (most recent remaining)
        assert reg.get_active()[0] == "mid"
    finally:
        reg.close()


def test_fallback_chain(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("first", "", "", "/proj/1")
        time.sleep(0.01)
        reg.register("second", "", "", "/proj/2")
        time.sleep(0.01)
        reg.register("third", "", "", "/proj/3")

        reg.unregister("third")
        assert reg.get_active()[0] == "second"

        reg.unregister("second")
        assert reg.get_active()[0] == "first"

        reg.unregister("first")
        assert reg.get_active() == (None, None)
    finally:
        reg.close()


def test_unregister_non_active_keeps_active(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("a", "", "", "/proj/a")
        time.sleep(0.01)
        reg.register("b", "", "", "/proj/b")
        reg.set_active("b")

        reg.unregister("a")
        assert reg.get_active()[0] == "b"
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 6. Validation: session_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "has space",  # space
        "with/slash",  # slash
        "semi;colon",  # semicolon
        "a" * 129,  # too long (>128)
        "new\nline",  # newline
        "tab\there",  # tab
        "dot.dot",  # dot not in allowed set
        "at@sign",  # @
    ],
)
def test_invalid_session_id(db_path, bad_id):
    reg = SessionRegistry(db_path)
    try:
        with pytest.raises(ValueError, match="Invalid session_id"):
            reg.register(bad_id, "", "", "/proj")
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 7. Validation: sty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_sty",
    [
        "has space",
        "semi;colon",
        "with/slash",
        "at@sign",
        "new\nline",
    ],
)
def test_invalid_sty(db_path, bad_sty):
    reg = SessionRegistry(db_path)
    try:
        with pytest.raises(ValueError, match="Invalid sty"):
            reg.register("valid-id", bad_sty, "", "/proj")
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 8. Validation: window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_window",
    [
        "abc",
        "1a",
        "-1",
        "3.14",
        "two words",
        " ",
    ],
)
def test_invalid_window(db_path, bad_window):
    reg = SessionRegistry(db_path)
    try:
        with pytest.raises(ValueError, match="Invalid window"):
            reg.register("valid-id", "", bad_window, "/proj")
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 9. Valid inputs — edge cases that must be accepted
# ---------------------------------------------------------------------------


def test_valid_session_id_with_hyphens_underscores(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("my-session_01", "", "", "/proj")
        assert "my-session_01" in reg.list_sessions()
    finally:
        reg.close()


def test_valid_session_id_max_length(db_path):
    reg = SessionRegistry(db_path)
    try:
        long_id = "a" * 128
        reg.register(long_id, "", "", "/proj")
        assert long_id in reg.list_sessions()
    finally:
        reg.close()


def test_valid_empty_sty_and_window(db_path):
    """Empty sty and window strings are allowed (session may not be in Screen)."""
    reg = SessionRegistry(db_path)
    try:
        reg.register("no-screen", "", "", "/proj")
        info = reg.get("no-screen")
        assert info is not None
        assert info["sty"] == ""
        assert info["window"] == ""
    finally:
        reg.close()


def test_valid_sty_with_dots_colons(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("sess1", "12345.pts-0.hostname", "0", "/proj")
        assert reg.get("sess1")["sty"] == "12345.pts-0.hostname"
    finally:
        reg.close()


def test_backend_none_default(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("sess1", "", "", "/proj")
        assert reg.get("sess1")["backend"] is None
    finally:
        reg.close()


def test_backend_explicit_value(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("sess1", "", "", "/proj", backend="screen")
        assert reg.get("sess1")["backend"] == "screen"
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 10. source field
# ---------------------------------------------------------------------------


def test_source_stored_and_retrieved(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("oc-sess", "", "", "/proj", source="opencode")
        info = reg.get("oc-sess")
        assert info is not None
        assert info["source"] == "opencode"
    finally:
        reg.close()


def test_source_defaults_to_none(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("claude-sess", "", "", "/proj")
        info = reg.get("claude-sess")
        assert info is not None
        assert info["source"] is None
    finally:
        reg.close()


def test_source_persists_across_restart(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("oc-sess", "", "", "/proj", source="opencode")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        info = reg2.get("oc-sess")
        assert info is not None
        assert info["source"] == "opencode"
    finally:
        reg2.close()


def test_schema_migration_adds_source_column(db_path):
    """Old DB without source column should be migrated transparently."""
    import sqlite3

    # Create a legacy DB without the source column
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE sessions ("
        "  session_id TEXT PRIMARY KEY,"
        "  sty TEXT, window TEXT, project TEXT, backend TEXT, registered_at REAL"
        ")"
    )
    conn.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-sess", "12345.pts-0", "0", "/old/project", "screen", 1000.0),
    )
    conn.commit()
    conn.close()

    # Opening with new registry should migrate without error
    reg = SessionRegistry(db_path)
    try:
        # Legacy session loaded, source should be None
        info = reg.get("legacy-sess")
        assert info is not None
        assert info["project"] == "/old/project"
        assert info["source"] is None

        # Can register new session with source
        reg.register("new-sess", "", "", "/new/project", source="opencode")
        assert reg.get("new-sess")["source"] == "opencode"  # type: ignore[index]
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 11. close() works without error
# ---------------------------------------------------------------------------


def test_close_fresh_registry(db_path):
    reg = SessionRegistry(db_path)
    reg.close()  # must not raise


def test_close_after_operations(db_path):
    reg = SessionRegistry(db_path)
    reg.register("x", "", "", "/proj")
    reg.set_active("x")
    reg.unregister("x")
    reg.close()  # must not raise


def test_double_close(db_path):
    """Closing twice should not raise (sqlite3 allows it)."""
    reg = SessionRegistry(db_path)
    reg.close()
    reg.close()  # must not raise


# ---------------------------------------------------------------------------
# 12. Stable ordering — registered_at and last_active behaviour
# ---------------------------------------------------------------------------


def test_reregister_same_sid_does_not_change_last_active(db_path):
    """Re-registering the same session_id must not hijack last_active."""
    reg = SessionRegistry(db_path)
    try:
        reg.register("s1", "", "", "/proj/1")
        time.sleep(0.01)
        reg.register("s2", "", "", "/proj/2")
        reg.set_active("s1")
        assert reg.get_active()[0] == "s1"

        # s2 re-registers (e.g. hook fires again) — s1 must stay active
        reg.register("s2", "", "", "/proj/2")
        assert reg.get_active()[0] == "s1"
    finally:
        reg.close()


def test_register_with_explicit_registered_at(db_path):
    """Passing registered_at preserves the slot's original position."""
    reg = SessionRegistry(db_path)
    try:
        old_time = time.time() - 100.0
        reg.register("sess", "", "", "/proj", registered_at=old_time)
        info = reg.get("sess")
        assert info is not None
        assert abs(info["registered_at"] - old_time) < 0.001
    finally:
        reg.close()


def test_reregister_new_sid_inherits_position(db_path):
    """When a new session_id replaces an old one (same project), ordering is preserved."""
    reg = SessionRegistry(db_path)
    try:
        old_time = time.time() - 100.0
        reg.register("old-sid", "", "", "/proj", registered_at=old_time)

        # Replace with new sid but preserve time (simulating what bridge does)
        reg.unregister("old-sid")
        reg.register("new-sid", "", "", "/proj", registered_at=old_time)

        info = reg.get("new-sid")
        assert info is not None
        assert abs(info["registered_at"] - old_time) < 0.001
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 13. plugin_version field
# ---------------------------------------------------------------------------


def test_plugin_version_stored_and_retrieved(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("pv-sess", "", "", "/proj", plugin_version="0.7.4")
        info = reg.get("pv-sess")
        assert info is not None
        assert info["plugin_version"] == "0.7.4"
    finally:
        reg.close()


def test_plugin_version_defaults_to_none(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("no-pv-sess", "", "", "/proj")
        info = reg.get("no-pv-sess")
        assert info is not None
        assert info["plugin_version"] is None
    finally:
        reg.close()


def test_plugin_version_persists_across_restart(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("pv-sess", "", "", "/proj", plugin_version="0.7.4")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        info = reg2.get("pv-sess")
        assert info is not None
        assert info["plugin_version"] == "0.7.4"
    finally:
        reg2.close()


def test_reregister_updates_plugin_version(db_path):
    """Re-registering with a new plugin_version should update the field."""
    reg = SessionRegistry(db_path)
    try:
        reg.register("pv-sess", "", "", "/proj", plugin_version="0.7.3")
        reg.register("pv-sess", "", "", "/proj", plugin_version="0.7.4")
        info = reg.get("pv-sess")
        assert info is not None
        assert info["plugin_version"] == "0.7.4"
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 14. agent_state / update_state
# ---------------------------------------------------------------------------


def test_agent_state_defaults_to_none(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("st-sess", "", "", "/proj")
        info = reg.get("st-sess")
        assert info is not None
        assert info["agent_state"] is None
    finally:
        reg.close()


def test_update_state_returns_true_for_existing(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("st-sess", "", "", "/proj")
        result = reg.update_state("st-sess", "idle")
        assert result is True
    finally:
        reg.close()


def test_update_state_returns_false_for_missing(db_path):
    reg = SessionRegistry(db_path)
    try:
        result = reg.update_state("nonexistent", "idle")
        assert result is False
    finally:
        reg.close()


def test_update_state_value_reflected_in_get(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("st-sess", "", "", "/proj")
        reg.update_state("st-sess", "running")
        info = reg.get("st-sess")
        assert info is not None
        assert info["agent_state"] == "running"
    finally:
        reg.close()


def test_update_state_persists_across_restart(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("st-sess", "", "", "/proj")
        reg1.update_state("st-sess", "idle")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        info = reg2.get("st-sess")
        assert info is not None
        assert info["agent_state"] == "idle"
    finally:
        reg2.close()


def test_reregister_preserves_agent_state(db_path):
    """Re-registering must not reset agent_state — agent may be running."""
    reg = SessionRegistry(db_path)
    try:
        reg.register("st-sess", "", "", "/proj")
        reg.update_state("st-sess", "running")
        reg.register("st-sess", "", "", "/proj")  # re-register
        info = reg.get("st-sess")
        assert info is not None
        assert info["agent_state"] == "running"
    finally:
        reg.close()


def test_agent_mode_defaults_to_none(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("mode-sess", "", "", "/proj")
        info = reg.get("mode-sess")
        assert info is not None
        assert info["agent_mode"] is None
    finally:
        reg.close()


def test_update_state_with_mode_sets_agent_mode(db_path):
    # Plugin sends emoji directly as mode value (e.g. 🟠 for coder agent)
    reg = SessionRegistry(db_path)
    try:
        reg.register("mode-sess", "", "", "/proj")
        reg.update_state("mode-sess", "running", mode="🟠")
        info = reg.get("mode-sess")
        assert info is not None
        assert info["agent_state"] == "running"
        assert info["agent_mode"] == "🟠"
    finally:
        reg.close()


def test_update_state_without_mode_leaves_agent_mode_unchanged(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.register("mode-sess", "", "", "/proj")
        reg.update_state("mode-sess", "running", mode="🔵")
        reg.update_state("mode-sess", "idle")  # no mode arg
        info = reg.get("mode-sess")
        assert info is not None
        assert info["agent_state"] == "idle"
        assert info["agent_mode"] == "🔵"  # preserved
    finally:
        reg.close()


def test_agent_mode_persists_across_restart(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("mode-sess", "", "", "/proj")
        reg1.update_state("mode-sess", "idle", mode="🟣")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        info = reg2.get("mode-sess")
        assert info is not None
        assert info["agent_mode"] == "🟣"
    finally:
        reg2.close()


def test_reregister_preserves_agent_mode(db_path):
    """Re-registering must not reset agent_mode."""
    reg = SessionRegistry(db_path)
    try:
        reg.register("mode-sess", "", "", "/proj")
        reg.update_state("mode-sess", "running", mode="🟠")
        reg.register("mode-sess", "", "", "/proj")  # re-register
        info = reg.get("mode-sess")
        assert info is not None
        assert info["agent_mode"] == "🟠"
    finally:
        reg.close()


def test_schema_migration_adds_plugin_version_and_agent_state(db_path):
    """Old DB without plugin_version/agent_state columns should be migrated."""
    import sqlite3

    # Create a DB that already has source but not the new columns
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE sessions ("
        "  session_id TEXT PRIMARY KEY,"
        "  sty TEXT, window TEXT, project TEXT, backend TEXT, source TEXT, registered_at REAL"
        ")"
    )
    conn.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-sess", "", "", "/old/project", None, None, 1000.0),
    )
    conn.commit()
    conn.close()

    # Registry must migrate transparently
    reg = SessionRegistry(db_path)
    try:
        info = reg.get("legacy-sess")
        assert info is not None
        assert info["plugin_version"] is None
        assert info["agent_state"] is None

        # New fields are writable after migration
        reg.update_state("legacy-sess", "idle")
        assert reg.get("legacy-sess")["agent_state"] == "idle"  # type: ignore[index]
    finally:
        reg.close()


def test_schema_migration_adds_agent_mode(db_path):
    """Old DB with plugin_version/agent_state but without agent_mode should be migrated."""
    import sqlite3

    # Create a DB that has source, plugin_version, agent_state but NOT agent_mode
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE sessions ("
        "  session_id TEXT PRIMARY KEY,"
        "  sty TEXT, window TEXT, project TEXT, backend TEXT, source TEXT, registered_at REAL,"
        "  plugin_version TEXT, agent_state TEXT"
        ")"
    )
    conn.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-sess", "", "", "/old/project", None, None, 1000.0, None, None),
    )
    conn.commit()
    conn.close()

    # Registry must migrate transparently
    reg = SessionRegistry(db_path)
    try:
        info = reg.get("legacy-sess")
        assert info is not None
        assert info["agent_mode"] is None  # defaulted to NULL after migration

        # agent_mode is writable after migration (plugin sends emoji directly)
        reg.update_state("legacy-sess", "idle", mode="⚪")
        assert reg.get("legacy-sess")["agent_mode"] == "⚪"  # type: ignore[index]
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# 15. inbox_put / inbox_drain / inbox_count
# ---------------------------------------------------------------------------


def test_inbox_drain_empty(db_path):
    """Draining an empty inbox returns an empty list without error."""
    reg = SessionRegistry(db_path)
    try:
        assert reg.inbox_drain("no-such-session") == []
    finally:
        reg.close()


def test_inbox_count_empty(db_path):
    reg = SessionRegistry(db_path)
    try:
        assert reg.inbox_count("no-such-session") == 0
    finally:
        reg.close()


def test_inbox_put_and_drain(db_path):
    """Messages put into the inbox are returned by drain in insertion order."""
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-a", "hello")
        reg.inbox_put("sess-a", "world")
        messages = reg.inbox_drain("sess-a")
        assert messages == ["hello", "world"]
    finally:
        reg.close()


def test_inbox_drain_is_destructive(db_path):
    """After draining, the inbox is empty."""
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-a", "msg1")
        reg.inbox_drain("sess-a")
        assert reg.inbox_drain("sess-a") == []
        assert reg.inbox_count("sess-a") == 0
    finally:
        reg.close()


def test_inbox_count_reflects_put(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-a", "m1")
        reg.inbox_put("sess-a", "m2")
        reg.inbox_put("sess-a", "m3")
        assert reg.inbox_count("sess-a") == 3
    finally:
        reg.close()


def test_inbox_isolated_per_session(db_path):
    """Messages for session A are not visible to session B."""
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-a", "for-a")
        reg.inbox_put("sess-b", "for-b")
        assert reg.inbox_drain("sess-a") == ["for-a"]
        assert reg.inbox_drain("sess-b") == ["for-b"]
    finally:
        reg.close()


def test_inbox_put_drops_oldest_when_full(db_path):
    """When inbox exceeds MAX_INBOX_SIZE, the oldest message is dropped."""
    from claude_xmpp_bridge.registry import MAX_INBOX_SIZE

    reg = SessionRegistry(db_path)
    try:
        for i in range(MAX_INBOX_SIZE):
            reg.inbox_put("sess-a", f"msg-{i}")
        assert reg.inbox_count("sess-a") == MAX_INBOX_SIZE

        # One more — should drop msg-0
        reg.inbox_put("sess-a", "overflow")
        assert reg.inbox_count("sess-a") == MAX_INBOX_SIZE

        messages = reg.inbox_drain("sess-a")
        assert messages[0] == "msg-1"  # msg-0 was dropped
        assert messages[-1] == "overflow"
    finally:
        reg.close()


def test_inbox_persists_across_restart(db_path):
    """Messages survive registry close/reopen (SQLite persistence)."""
    reg1 = SessionRegistry(db_path)
    try:
        reg1.inbox_put("sess-a", "persistent-msg")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        messages = reg2.inbox_drain("sess-a")
        assert messages == ["persistent-msg"]
    finally:
        reg2.close()


def test_inbox_from_session_stored(db_path):
    """from_session metadata is stored in the inbox row (verified via DB)."""
    import sqlite3

    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-b", "hi there", from_session="sess-a")
    finally:
        reg.close()

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT from_session, message FROM inbox WHERE to_session = 'sess-b'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "sess-a"
    assert row[1] == "hi there"


def test_inbox_drain_with_senders_preserves_metadata(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-b", "one", from_session="sess-a")
        reg.inbox_put("sess-b", "two")
        rows = reg.inbox_drain_with_senders("sess-b")
        assert rows == [("one", "sess-a"), ("two", None)]
    finally:
        reg.close()


def test_inbox_put_stores_source_and_message_type(db_path):
    import sqlite3

    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put(
            "sess-b", "hello", from_session="sess-a", source_type="agent", message_type="relay"
        )
    finally:
        reg.close()

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT source_type, message_type FROM inbox WHERE to_session = 'sess-b'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "agent"
    assert row[1] == "relay"


def test_inbox_drain_full_returns_all_metadata(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put(
            "sess-b", "msg1", from_session="sess-a", source_type="agent", message_type="relay"
        )
        reg.inbox_put("sess-b", "msg2", source_type="system", message_type="broadcast")
        rows = reg.inbox_drain_full("sess-b")
        assert len(rows) == 2
        assert rows[0]["message"] == "msg1"
        assert rows[0]["from_session"] == "sess-a"
        assert rows[0]["source_type"] == "agent"
        assert rows[0]["message_type"] == "relay"
        assert isinstance(rows[0]["created_at"], float)
        assert rows[1]["message"] == "msg2"
        assert rows[1]["from_session"] is None
        assert rows[1]["source_type"] == "system"
        assert rows[1]["message_type"] == "broadcast"
    finally:
        reg.close()


def test_inbox_drain_full_empty(db_path):
    reg = SessionRegistry(db_path)
    try:
        rows = reg.inbox_drain_full("nonexistent")
        assert rows == []
    finally:
        reg.close()


def test_inbox_drain_full_deletes_after_drain(db_path):
    reg = SessionRegistry(db_path)
    try:
        reg.inbox_put("sess-b", "msg1", source_type="agent", message_type="relay")
        rows = reg.inbox_drain_full("sess-b")
        assert len(rows) == 1
        # Second drain should be empty
        rows2 = reg.inbox_drain_full("sess-b")
        assert rows2 == []
    finally:
        reg.close()


def test_inbox_migration_adds_columns_to_existing_db(db_path):
    """Verify ALTER TABLE migration adds source_type/message_type columns."""
    import sqlite3

    # Create a DB without the new columns (simulate old schema)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS inbox ("
        "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  to_session   TEXT    NOT NULL,"
        "  from_session TEXT,"
        "  message      TEXT    NOT NULL,"
        "  created_at   REAL    NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO inbox (to_session, from_session, message, created_at) VALUES (?, ?, ?, ?)",
        ("sess-b", "sess-a", "old-msg", 1000.0),
    )
    conn.commit()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(inbox)")}
    assert "source_type" not in cols
    assert "message_type" not in cols
    conn.close()

    # Opening SessionRegistry should run migrations
    reg = SessionRegistry(db_path)
    try:
        rows = reg.inbox_drain_full("sess-b")
        assert len(rows) == 1
        assert rows[0]["message"] == "old-msg"
        assert rows[0]["from_session"] == "sess-a"
        # Migrated rows have NULL for new columns
        assert rows[0]["source_type"] is None
        assert rows[0]["message_type"] is None
    finally:
        reg.close()


def test_last_agent_sender_persists_across_restart(db_path):
    reg1 = SessionRegistry(db_path)
    try:
        reg1.register("sess-b", "", "", "/tmp/proj")
        assert reg1.set_last_agent_sender("sess-b", "sess-a") is True
        assert reg1.get_last_agent_sender("sess-b") == "sess-a"
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        assert reg2.get_last_agent_sender("sess-b") == "sess-a"
    finally:
        reg2.close()


# ---------------------------------------------------------------------------
# Task delegation CRUD
# ---------------------------------------------------------------------------


def test_task_create_and_get(db_path):
    """task_create stores a record retrievable via task_get."""
    reg = SessionRegistry(db_path)
    try:
        task = reg.task_create(
            task_id="abc123456789",
            from_session="sess-a",
            to_session="sess-b",
            description="Build the widget",
            context="See PR #42",
        )
        assert task["task_id"] == "abc123456789"
        assert task["from_session"] == "sess-a"
        assert task["to_session"] == "sess-b"
        assert task["description"] == "Build the widget"
        assert task["context"] == "See PR #42"
        assert task["status"] == "pending"
        assert task["result"] is None
        assert isinstance(task["created_at"], float)
        assert task["created_at"] == task["updated_at"]

        fetched = reg.task_get("abc123456789")
        assert fetched is not None
        assert fetched["task_id"] == "abc123456789"
        assert fetched["description"] == "Build the widget"
        assert fetched["context"] == "See PR #42"
        assert fetched["status"] == "pending"
    finally:
        reg.close()


def test_task_create_sets_pending_status(db_path):
    """Newly created tasks always have status='pending'."""
    reg = SessionRegistry(db_path)
    try:
        task = reg.task_create(
            task_id="pend000000aa",
            from_session="s-1",
            to_session="s-2",
            description="Do stuff",
        )
        assert task["status"] == "pending"
    finally:
        reg.close()


def test_task_create_without_context(db_path):
    """context defaults to None when omitted."""
    reg = SessionRegistry(db_path)
    try:
        task = reg.task_create(
            task_id="noctx0000000",
            from_session="s-1",
            to_session="s-2",
            description="No context task",
        )
        assert task["context"] is None
        fetched = reg.task_get("noctx0000000")
        assert fetched is not None
        assert fetched["context"] is None
    finally:
        reg.close()


def test_task_update_status_completed(db_path):
    """task_update_status transitions to completed with a result."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(
            task_id="upd000000001",
            from_session="s-a",
            to_session="s-b",
            description="Run tests",
        )
        updated = reg.task_update_status("upd000000001", "completed", result="All 42 tests passed")
        assert updated is not None
        assert updated["status"] == "completed"
        assert updated["result"] == "All 42 tests passed"
        assert updated["updated_at"] >= updated["created_at"]

        # Verify persisted
        fetched = reg.task_get("upd000000001")
        assert fetched is not None
        assert fetched["status"] == "completed"
        assert fetched["result"] == "All 42 tests passed"
    finally:
        reg.close()


def test_task_update_status_nonexistent_returns_none(db_path):
    """task_update_status with a bad ID returns None."""
    reg = SessionRegistry(db_path)
    try:
        result = reg.task_update_status("nonexistent_id", "completed")
        assert result is None
    finally:
        reg.close()


def test_task_get_nonexistent_returns_none(db_path):
    """task_get with unknown ID returns None."""
    reg = SessionRegistry(db_path)
    try:
        result = reg.task_get("does_not_exist")
        assert result is None
    finally:
        reg.close()


def test_task_list_all(db_path):
    """task_list without filters returns all tasks in created_at DESC order."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(task_id="t001", from_session="a", to_session="b", description="first")
        reg.task_create(task_id="t002", from_session="a", to_session="c", description="second")
        reg.task_create(task_id="t003", from_session="c", to_session="a", description="third")

        tasks = reg.task_list()
        assert len(tasks) == 3
        # DESC order — newest first
        assert tasks[0]["task_id"] == "t003"
        assert tasks[1]["task_id"] == "t002"
        assert tasks[2]["task_id"] == "t001"
    finally:
        reg.close()


def test_task_list_filter_by_session_role_from(db_path):
    """task_list with role='from' filters by from_session."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(task_id="tf01", from_session="alice", to_session="bob", description="d1")
        reg.task_create(task_id="tf02", from_session="bob", to_session="alice", description="d2")
        reg.task_create(task_id="tf03", from_session="alice", to_session="charlie", description="d3")

        tasks = reg.task_list(session_id="alice", role="from")
        assert len(tasks) == 2
        ids = {t["task_id"] for t in tasks}
        assert ids == {"tf01", "tf03"}
    finally:
        reg.close()


def test_task_list_filter_by_session_role_to(db_path):
    """task_list with role='to' filters by to_session."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(task_id="tt01", from_session="alice", to_session="bob", description="d1")
        reg.task_create(task_id="tt02", from_session="bob", to_session="alice", description="d2")

        tasks = reg.task_list(session_id="bob", role="to")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "tt01"
    finally:
        reg.close()


def test_task_list_filter_by_session_role_both(db_path):
    """task_list with role='both' matches from_session OR to_session."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(task_id="tb01", from_session="alice", to_session="bob", description="d1")
        reg.task_create(task_id="tb02", from_session="bob", to_session="charlie", description="d2")
        reg.task_create(task_id="tb03", from_session="charlie", to_session="dave", description="d3")

        tasks = reg.task_list(session_id="bob", role="both")
        assert len(tasks) == 2
        ids = {t["task_id"] for t in tasks}
        assert ids == {"tb01", "tb02"}
    finally:
        reg.close()


def test_task_list_filter_by_status(db_path):
    """task_list filters by status when specified."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(task_id="ts01", from_session="a", to_session="b", description="d1")
        reg.task_create(task_id="ts02", from_session="a", to_session="c", description="d2")
        reg.task_update_status("ts02", "completed", result="done")

        tasks = reg.task_list(status="pending")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "ts01"

        tasks = reg.task_list(status="completed")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "ts02"
    finally:
        reg.close()


def test_task_list_combined_filters(db_path):
    """task_list with both session_id and status filters."""
    reg = SessionRegistry(db_path)
    try:
        reg.task_create(task_id="tc01", from_session="a", to_session="b", description="d1")
        reg.task_create(task_id="tc02", from_session="a", to_session="c", description="d2")
        reg.task_create(task_id="tc03", from_session="b", to_session="a", description="d3")
        reg.task_update_status("tc01", "completed")
        reg.task_update_status("tc03", "completed")

        # session_id=a, role=from, status=completed → only tc01
        tasks = reg.task_list(session_id="a", role="from", status="completed")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "tc01"

        # session_id=a, role=both, status=completed → tc01 and tc03
        tasks = reg.task_list(session_id="a", role="both", status="completed")
        assert len(tasks) == 2
        ids = {t["task_id"] for t in tasks}
        assert ids == {"tc01", "tc03"}
    finally:
        reg.close()


def test_task_persists_across_restart(db_path):
    """Tasks survive a registry close/reopen cycle."""
    reg1 = SessionRegistry(db_path)
    try:
        reg1.task_create(
            task_id="persist00001",
            from_session="s-a",
            to_session="s-b",
            description="Persist me",
            context="ctx",
        )
        reg1.task_update_status("persist00001", "accepted")
    finally:
        reg1.close()

    reg2 = SessionRegistry(db_path)
    try:
        fetched = reg2.task_get("persist00001")
        assert fetched is not None
        assert fetched["task_id"] == "persist00001"
        assert fetched["description"] == "Persist me"
        assert fetched["context"] == "ctx"
        assert fetched["status"] == "accepted"
    finally:
        reg2.close()
