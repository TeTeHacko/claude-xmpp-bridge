"""Integration tests for claude-xmpp-client ↔ bridge socket protocol.

These tests spin up a real bridge socket server (in-process, with mocked XMPP)
and invoke the actual ``claude-xmpp-client`` binary as a subprocess.  They
verify the *behaviour* that the OpenCode plugin relies on:

- ``state`` for an unknown session → exit 1, stderr contains "Error:"
- ``state`` for a known session   → exit 0
- ``register`` + ``state``        → exit 0 (happy path)
- session lost from DB (bridge restart simulation) → ``state`` exit 1,
  ``register`` again → ``state`` exit 0  (re-registration flow)
- periodic re-register timer: after session is removed from DB the timer
  fires and the session reappears (requires XMPP_BRIDGE_REREG_INTERVAL_MS
  env var to be set to a low value in the plugin; here we test the
  bridge-side: register → delete → register again → state OK)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_xmpp_bridge.bridge import XMPPBridge
from claude_xmpp_bridge.config import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> Config:
    return Config(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
        socket_path=tmp_path / "test.sock",
        db_path=tmp_path / "test.db",
        messages_file=None,
        socket_token=None,
        mcp_port=0,  # disable MCP HTTP server
    )


def _client_bin() -> str:
    """Return path to the installed claude-xmpp-client binary."""
    return str(Path(sys.executable).parent / "claude-xmpp-client")


def _reg_json(sid: str, window: str = "6") -> str:
    """Build a register JSON payload for the given session ID."""
    return json.dumps(
        {
            "session_id": sid,
            "sty": "5757.pts-0",
            "window": window,
            "project": "/home/user/proj",
            "backend": "screen",
        }
    )


def _state_json(sid: str, state: str = "idle", mode: str | None = None) -> str:
    """Build a state JSON payload."""
    d: dict[str, str] = {"session_id": sid, "state": state}
    if mode is not None:
        d["mode"] = mode
    return json.dumps(d)


async def _run_client(socket_path: Path, *args: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
    """Run claude-xmpp-client in a thread executor so it doesn't block the event loop.

    The bridge socket server runs in the same event loop as the test.  A plain
    subprocess.run() would block the loop and prevent the server from handling
    the incoming connection — causing a 5 s timeout.  run_in_executor keeps the
    loop free to process socket events while the subprocess runs.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            [_client_bin(), "--socket-path", str(socket_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        ),
    )


def _remove_session(b: XMPPBridge, sid: str) -> None:
    """Remove a session from the registry in-place (simulates bridge restart)."""
    del b.registry.sessions[sid]
    b.registry._db.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    b.registry._db.commit()


# ---------------------------------------------------------------------------
# Fixture: running bridge socket server (XMPP mocked out)
# ---------------------------------------------------------------------------


@pytest.fixture
async def bridge(tmp_path):
    """Start a bridge socket server and yield (bridge, config). Tears down after test."""
    with patch("claude_xmpp_bridge.bridge.XMPPConnection") as MockXMPP:
        conn = MagicMock()
        conn.connected = asyncio.Event()
        conn.connected.set()
        conn.on_message.side_effect = lambda cb: None
        MockXMPP.return_value = conn

        config = _make_config(tmp_path)
        b = XMPPBridge(config)
        await b.socket_server.start()
        try:
            yield b, config
        finally:
            await b.socket_server.stop()
            b.registry.close()


# ---------------------------------------------------------------------------
# TestStateExitCodes
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStateExitCodes:
    """claude-xmpp-client state must return correct exit codes."""

    async def test_state_unknown_session_exits_nonzero(self, bridge):
        """state for a session the bridge doesn't know → exit 1, stderr has 'Error:'."""
        b, cfg = bridge
        result = await _run_client(
            cfg.socket_path,
            "state",
            _state_json("nonexistent-session"),
        )
        assert result.returncode != 0, "expected non-zero exit for unknown session"
        assert "Error:" in result.stderr, f"expected 'Error:' in stderr, got: {result.stderr!r}"
        assert "session not found" in result.stderr.lower(), (
            f"expected 'session not found' in stderr, got: {result.stderr!r}"
        )

    async def test_state_known_session_exits_zero(self, bridge):
        """state for a registered session → exit 0."""
        b, cfg = bridge

        reg = await _run_client(cfg.socket_path, "register", _reg_json("sess-ok", window="1"))
        assert reg.returncode == 0, f"register failed: {reg.stderr}"

        result = await _run_client(cfg.socket_path, "state", _state_json("sess-ok"))
        assert result.returncode == 0, f"expected exit 0, got {result.returncode}: {result.stderr}"

    async def test_state_bridge_not_running_exits_nonzero(self, tmp_path):
        """state when bridge is not running → exit non-zero."""
        result = await _run_client(
            tmp_path / "nonexistent.sock",
            "state",
            _state_json("any"),
        )
        assert result.returncode != 0, f"expected non-zero exit when bridge not running, got {result.returncode}"
        assert "Error:" in result.stderr, f"expected 'Error:' in stderr, got: {result.stderr!r}"

    async def test_state_updates_agent_mode_in_registry(self, bridge):
        """state with mode field → bridge stores agent_mode in registry."""
        b, cfg = bridge

        await _run_client(cfg.socket_path, "register", _reg_json("sess-mode", window="2"))
        result = await _run_client(cfg.socket_path, "state", _state_json("sess-mode", state="running", mode="🟠"))
        assert result.returncode == 0
        assert b.registry.sessions["sess-mode"]["agent_mode"] == "🟠"
        assert b.registry.sessions["sess-mode"]["agent_state"] == "running"


# ---------------------------------------------------------------------------
# TestReregisterAfterBridgeRestart
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReregisterAfterBridgeRestart:
    """Simulate bridge restart: session disappears from DB, plugin must re-register."""

    async def test_register_then_state_ok(self, bridge):
        """Basic happy path: register → state → exit 0."""
        b, cfg = bridge

        reg = await _run_client(cfg.socket_path, "register", _reg_json("sess-rereg"))
        assert reg.returncode == 0, f"register failed: {reg.stderr}"
        assert "sess-rereg" in b.registry.sessions

        state = await _run_client(cfg.socket_path, "state", _state_json("sess-rereg"))
        assert state.returncode == 0, f"state failed after register: {state.stderr}"

    async def test_session_lost_state_fails_reregister_restores(self, bridge):
        """Core re-registration flow:
        1. register → state OK
        2. remove session from registry (simulates bridge restart / cleanup)
        3. state → exit 1 (bridge doesn't know session)
        4. register again → exit 0
        5. state → exit 0 (session restored)
        """
        b, cfg = bridge
        sid = "sess-lost"

        # Step 1: register + verify state OK
        reg = await _run_client(cfg.socket_path, "register", _reg_json(sid))
        assert reg.returncode == 0, f"register failed: {reg.stderr}"
        state1 = await _run_client(cfg.socket_path, "state", _state_json(sid))
        assert state1.returncode == 0, f"initial state failed: {state1.stderr}"

        # Step 2: simulate bridge restart — remove session from registry directly
        _remove_session(b, sid)
        assert sid not in b.registry.sessions

        # Step 3: state must now fail (bridge doesn't know session)
        state2 = await _run_client(cfg.socket_path, "state", _state_json(sid))
        assert state2.returncode != 0, "expected non-zero exit after session removed"
        assert "Error:" in state2.stderr

        # Step 4: re-register
        rereg = await _run_client(cfg.socket_path, "register", _reg_json(sid))
        assert rereg.returncode == 0, f"re-register failed: {rereg.stderr}"
        assert sid in b.registry.sessions

        # Step 5: state must succeed again
        state3 = await _run_client(cfg.socket_path, "state", _state_json(sid))
        assert state3.returncode == 0, f"state failed after re-register: {state3.stderr}"

    async def test_reregister_clears_stale_agent_state(self, bridge):
        """After a fresh re-register (no prior DB entry), agent_state/mode start as None.
        The plugin then sends a state update to restore them.
        """
        b, cfg = bridge
        sid = "sess-preserve"

        # Register and set state+mode
        await _run_client(cfg.socket_path, "register", _reg_json(sid, window="7"))
        await _run_client(cfg.socket_path, "state", _state_json(sid, state="running", mode="🔵"))
        assert b.registry.sessions[sid]["agent_state"] == "running"
        assert b.registry.sessions[sid]["agent_mode"] == "🔵"

        # Simulate restart: remove from registry
        _remove_session(b, sid)

        # Re-register (plugin sends register without state/mode — bridge starts fresh)
        await _run_client(cfg.socket_path, "register", _reg_json(sid, window="7"))
        assert sid in b.registry.sessions
        # agent_state/mode are None after fresh register (no prior state in DB)
        assert b.registry.sessions[sid]["agent_state"] is None
        assert b.registry.sessions[sid]["agent_mode"] is None

        # Plugin then sends state to update
        await _run_client(cfg.socket_path, "state", _state_json(sid, state="idle", mode="🔵"))
        assert b.registry.sessions[sid]["agent_state"] == "idle"
        assert b.registry.sessions[sid]["agent_mode"] == "🔵"

    async def test_multiple_sessions_independent(self, bridge):
        """Removing one session must not affect others."""
        b, cfg = bridge

        for i in range(3):
            r = await _run_client(cfg.socket_path, "register", _reg_json(f"sess-multi-{i}", window=str(i)))
            assert r.returncode == 0, f"register sess-multi-{i} failed: {r.stderr}"

        # Remove middle session
        _remove_session(b, "sess-multi-1")

        # Others still work
        for i in (0, 2):
            result = await _run_client(cfg.socket_path, "state", _state_json(f"sess-multi-{i}"))
            assert result.returncode == 0, f"sess-multi-{i} state failed: {result.stderr}"

        # Removed one fails
        result = await _run_client(cfg.socket_path, "state", _state_json("sess-multi-1"))
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# TestReregisterTimer
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReregisterTimer:
    """The periodic re-register timer (XMPP_BRIDGE_REREG_INTERVAL_MS) must
    restore a session that was removed from the bridge DB.

    We test the bridge-side contract: after a session is removed from the
    registry, a subsequent register call restores it.  The plugin timer
    is parametrised via XMPP_BRIDGE_REREG_INTERVAL_MS — this test verifies
    that the env var is present in the plugin source so the timer interval
    can be overridden in tests/CI.
    """

    def test_plugin_rereg_interval_is_configurable(self):
        """Plugin must read REREG_INTERVAL_MS from XMPP_BRIDGE_REREG_INTERVAL_MS env var."""
        from test_setup import _find_opencode_dir  # type: ignore[import]

        plugin_dir = _find_opencode_dir()
        assert plugin_dir is not None, "opencode plugin dir not found"
        text = (plugin_dir / "plugins" / "xmpp-bridge.js").read_text()
        assert "XMPP_BRIDGE_REREG_INTERVAL_MS" in text, (
            "Plugin must read REREG_INTERVAL_MS from XMPP_BRIDGE_REREG_INTERVAL_MS env var "
            "so tests can override the interval without waiting 90 seconds"
        )

    async def test_rereg_timer_restores_session(self, bridge):
        """Simulate what the timer does: after session is lost, register + state succeeds.

        The actual timer fires in the plugin (JS), not in Python.  Here we test
        the bridge-side contract that the timer relies on:
          1. register → state OK
          2. session removed (bridge restart simulation)
          3. register (timer fires) → state OK again
        """
        b, cfg = bridge
        sid = "sess-timer"

        await _run_client(cfg.socket_path, "register", _reg_json(sid))
        r = await _run_client(cfg.socket_path, "state", _state_json(sid))
        assert r.returncode == 0, f"initial state failed: {r.stderr}"

        # Simulate bridge restart
        _remove_session(b, sid)

        # Timer fires: plugin calls register
        rereg = await _run_client(cfg.socket_path, "register", _reg_json(sid))
        assert rereg.returncode == 0, f"timer re-register failed: {rereg.stderr}"

        # State must work again
        r2 = await _run_client(cfg.socket_path, "state", _state_json(sid))
        assert r2.returncode == 0, f"state after timer re-register failed: {r2.stderr}"


# ---------------------------------------------------------------------------
# TestClientSubcommandsWithoutBridge
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestClientSubcommandsWithoutBridge:
    """Verify exit codes and stderr for every client subcommand when the bridge
    is not running (socket does not exist).

    Plugin sandbox contract:
      - state   → exit non-zero + "Error:" in stderr  (plugin detects failure)
      - register → exit 0, empty stderr               (plugin ignores silently)
      - unregister → exit 0, empty stderr             (plugin ignores silently)

    The asymmetry is intentional: state is the heartbeat that drives
    reregisterIfNeeded; register/unregister are fire-and-forget.
    """

    async def test_state_without_bridge_exits_nonzero(self, tmp_path):
        """state without bridge → exit non-zero, stderr contains 'Error:'."""
        result = await _run_client(
            tmp_path / "no.sock",
            "state",
            _state_json("any-session"),
        )
        assert result.returncode != 0, f"state must exit non-zero when bridge not running, got {result.returncode}"
        assert "Error:" in result.stderr, f"state must print 'Error:' to stderr, got: {result.stderr!r}"

    async def test_register_without_bridge_exits_zero_silently(self, tmp_path):
        """register without bridge → exit 0, no stderr output.

        The plugin calls register fire-and-forget; a non-zero exit here would
        cause unnecessary noise in sandbox environments.
        """
        result = await _run_client(
            tmp_path / "no.sock",
            "register",
            _reg_json("any-session"),
        )
        assert result.returncode == 0, (
            f"register must exit 0 when bridge not running (silent skip), got {result.returncode}: {result.stderr!r}"
        )
        assert result.stderr.strip() == "", (
            f"register must produce no stderr when bridge not running, got: {result.stderr!r}"
        )

    async def test_unregister_without_bridge_exits_zero_silently(self, tmp_path):
        """unregister without bridge → exit 0, no stderr output."""
        result = await _run_client(
            tmp_path / "no.sock",
            "unregister",
            "any-session-id",
        )
        assert result.returncode == 0, (
            f"unregister must exit 0 when bridge not running (silent skip), got {result.returncode}: {result.stderr!r}"
        )
        assert result.stderr.strip() == "", (
            f"unregister must produce no stderr when bridge not running, got: {result.stderr!r}"
        )
