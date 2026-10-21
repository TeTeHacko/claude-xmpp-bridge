"""Claude Code hook delivery (v0.11.0): inbox + hooks instead of typing messages.

Verified manually on a throwaway Claude Code 2.1.296 session first:
PostToolUse additionalContext, Stop decision=block and UserPromptSubmit
additionalContext all reach the model and are shown as hook output.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from helpers import make_mock_conn as _make_mock_conn

from claude_xmpp_bridge import cli
from claude_xmpp_bridge.bridge import XMPPBridge
from claude_xmpp_bridge.config import Config
from claude_xmpp_bridge.messages import format_generated_agent_message


def _config(tmp_path: Path) -> Config:
    return Config(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
        socket_path=tmp_path / "test.sock",
        db_path=tmp_path / "test.db",
        messages_file=None,
    )


def _bridge(MockXMPP, tmp_path, pv="hook+inbox", source="claude-code"):
    conn, _ = _make_mock_conn(MockXMPP)
    bridge = XMPPBridge(_config(tmp_path))
    bridge.registry.register(
        session_id="s1", sty="123.pts-0", window="6", project="/p", backend="screen", source=source, plugin_version=pv
    )
    return bridge, conn


def _env(body="hello", mid="aaaabbbbcccc"):
    return format_generated_agent_message(msg_type="relay", message=body, message_id=mid, to_session_id="s1")


class TestCapability:
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_detection(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        info = bridge.registry.get("s1")
        assert bridge._hook_inbox_capable(info)
        for pv, src in (("hook", "claude-code"), ("hook+inbox", "opencode"), (None, "claude-code")):
            assert not bridge._hook_inbox_capable({**info, "plugin_version": pv, "source": src})
        assert not bridge._hook_inbox_capable({**info, "backend": None})
        bridge.registry.close()


class TestDeliverViaHook:
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_idle_session_gets_short_trigger_and_inbox(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        bridge.registry.update_state("s1", "idle")
        mux = MagicMock()
        mux.send_text = AsyncMock(return_value=True)
        with (
            patch("claude_xmpp_bridge.bridge.get_multiplexer", return_value=mux),
            patch.object(bridge, "_hook_watchdog", AsyncMock()),
        ):
            ok = await bridge._stuff_to_session("s1", bridge.registry.get("s1"), _env("x" * 3000))
        assert ok is True
        # only the trigger line is typed - never the message
        mux.send_text.assert_awaited_once()
        assert mux.send_text.await_args.args[2] == bridge.messages.hook_trigger
        assert bridge.registry.inbox_count("s1") == 1
        assert bridge.verify_message("aaaabbbbcccc")["found"] is True
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_running_session_not_typed_into(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        bridge.registry.update_state("s1", "running")
        mux = MagicMock()
        mux.send_text = AsyncMock(return_value=True)
        with (
            patch("claude_xmpp_bridge.bridge.get_multiplexer", return_value=mux),
            patch.object(bridge, "_hook_watchdog", AsyncMock()),
        ):
            await bridge._nudge_session("s1", bridge.registry.get("s1"), _env())
        mux.send_text.assert_not_awaited()
        mux.send_nudge.assert_not_called()
        assert bridge.registry.inbox_count("s1") == 1
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_old_hook_session_keeps_screen_path(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path, pv="hook")
        mux = MagicMock()
        mux.send_text = AsyncMock(return_value=True)
        with patch("claude_xmpp_bridge.bridge.get_multiplexer", return_value=mux):
            await bridge._stuff_to_session("s1", bridge.registry.get("s1"), "plain text")
        assert mux.send_text.await_args.args[2] == "plain text"
        assert bridge.registry.inbox_count("s1") == 0
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_watchdog_alerts_when_nobody_picks_up(self, MockXMPP, tmp_path):
        bridge, conn = _bridge(MockXMPP, tmp_path)
        bridge._HOOK_RETRIGGER_AFTER = 0
        bridge._HOOK_ALERT_AFTER = 0
        bridge.registry.update_state("s1", "running")
        bridge.registry.inbox_put("s1", _env())
        await bridge._hook_watchdog("s1")
        conn.send.assert_called_once()
        assert "not picked up" in conn.send.call_args.args[1]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_watchdog_quiet_when_delivered(self, MockXMPP, tmp_path):
        bridge, conn = _bridge(MockXMPP, tmp_path)
        bridge._HOOK_RETRIGGER_AFTER = 0
        bridge._HOOK_ALERT_AFTER = 0
        await bridge._hook_watchdog("s1")
        conn.send.assert_not_called()
        bridge.registry.close()


class TestHookCommand:
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_peek_then_ack(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        bridge.registry.inbox_put("s1", _env("first", "111111111111"))
        bridge.registry.inbox_put("s1", _env("second", "222222222222"))
        r = bridge._handle_hook({"session_id": "s1", "event": "PostToolUse"})
        assert r["ok"] and len(r["ids"]) == 2
        assert "not typed by the user" in r["context"]
        assert "first" in r["context"] and "second" in r["context"]
        # not removed until acked
        assert bridge.registry.inbox_count("s1") == 2
        assert bridge.registry.get("s1")["agent_state"] == "running"
        assert bridge._handle_hook({"session_id": "s1", "ack": r["ids"]})["acked"] == 2
        assert bridge.registry.inbox_count("s1") == 0
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_stop_without_messages_marks_idle(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        bridge.registry.update_state("s1", "running")
        r = bridge._handle_hook({"session_id": "s1", "event": "Stop"})
        assert r["context"] is None
        assert bridge.registry.get("s1")["agent_state"] == "idle"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_stop_with_messages_stays_running(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        bridge.registry.inbox_put("s1", _env())
        r = bridge._handle_hook({"session_id": "s1", "event": "Stop"})
        assert r["context"]
        assert bridge.registry.get("s1")["agent_state"] == "running"
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_rejects_unknown(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        assert bridge._handle_hook({"session_id": "nope", "event": "Stop"})["ok"] is False
        assert bridge._handle_hook({"session_id": "s1", "event": "PreToolUse"})["ok"] is False
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_ack_only_own_session_rows(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path)
        bridge.registry.register(session_id="s2", sty="x", window="7", project="/q", backend="screen")
        bridge.registry.inbox_put("s2", "for s2")
        other_ids = [i for i, _ in bridge.registry.inbox_peek("s2")]
        assert bridge._handle_hook({"session_id": "s1", "ack": other_ids})["acked"] == 0
        assert bridge.registry.inbox_count("s2") == 1
        bridge.registry.close()


class TestRunHookCli:
    def _run(self, event, stdin, responses):
        calls = []

        def fake_send(req, socket_path, socket_timeout=None):
            calls.append(req)
            return responses.pop(0) if responses else None

        out = io.StringIO()
        with (
            patch("claude_xmpp_bridge.client.send_to_bridge", side_effect=fake_send),
            patch("sys.stdin", io.StringIO(stdin)),
            patch("sys.stdout", out),
        ):
            rc = cli.run_hook(event, Path("/nonexistent"))
        return rc, out.getvalue(), calls

    def test_post_tool_use_outputs_context_then_acks(self):
        rc, out, calls = self._run(
            "PostToolUse",
            json.dumps({"session_id": "s1"}),
            [{"ok": True, "context": "MSG", "ids": [5]}, {"ok": True, "acked": 1}],
        )
        assert rc == 0
        assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "MSG"}}
        assert calls[1] == {"cmd": "hook", "session_id": "s1", "event": "PostToolUse", "ack": [5]}

    def test_stop_blocks_with_reason(self):
        _, out, _ = self._run(
            "Stop", json.dumps({"session_id": "s1"}), [{"ok": True, "context": "MSG", "ids": [1]}, {}]
        )
        assert json.loads(out) == {"decision": "block", "reason": "MSG"}

    def test_nothing_pending_prints_nothing_and_no_ack(self):
        rc, out, calls = self._run("Stop", json.dumps({"session_id": "s1"}), [{"ok": True, "context": None, "ids": []}])
        assert rc == 0 and out == "" and len(calls) == 1

    def test_bridge_down_is_silent(self):
        rc, out, _ = self._run("PostToolUse", json.dumps({"session_id": "s1"}), [])
        assert rc == 0 and out == ""

    def test_garbage_stdin_is_silent(self):
        rc, out, calls = self._run("PostToolUse", "not json", [])
        assert rc == 0 and out == "" and calls == []


class TestAutoUpgrade:
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_first_hook_call_upgrades_plain_hook_session(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path, pv="hook")
        assert not bridge._hook_inbox_capable(bridge.registry.get("s1"))
        bridge._handle_hook({"session_id": "s1", "event": "PostToolUse"})
        assert bridge.registry.get("s1")["plugin_version"] == "hook+inbox"
        assert bridge._hook_inbox_capable(bridge.registry.get("s1"))
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    def test_opencode_not_upgraded(self, MockXMPP, tmp_path):
        bridge, _ = _bridge(MockXMPP, tmp_path, pv="0.9.19", source="opencode")
        bridge._handle_hook({"session_id": "s1", "event": "PostToolUse"})
        assert bridge.registry.get("s1")["plugin_version"] == "0.9.19"
        bridge.registry.close()
