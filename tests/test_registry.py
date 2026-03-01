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
        "",                    # empty
        "has space",           # space
        "with/slash",          # slash
        "semi;colon",          # semicolon
        "a" * 129,            # too long (>128)
        "new\nline",           # newline
        "tab\there",           # tab
        "dot.dot",             # dot not in allowed set
        "at@sign",             # @
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
# 10. close() works without error
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
