"""Tests for SessionRegistry — lifecycle, persistence, ordering, validation."""

from __future__ import annotations

import time

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
