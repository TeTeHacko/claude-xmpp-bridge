"""Tests for cli module — argument parsing and entry points."""

from __future__ import annotations

import sys

import pytest

from claude_xmpp_bridge import __version__
from claude_xmpp_bridge.cli import ask_main, bridge_main, client_main, notify_main


class TestHelpExitsZero:
    """--help must exit 0 for all entry points."""

    def test_bridge_main_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-bridge", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            bridge_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "XMPP bridge daemon" in captured.out

    def test_client_main_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Client for claude-xmpp-bridge" in captured.out

    def test_notify_main_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-notify", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            notify_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "XMPP notification" in captured.out

    def test_ask_main_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-ask", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            ask_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "wait for reply" in captured.out


class TestVersionExitsZero:
    """--version must exit 0 for all entry points."""

    def test_bridge_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-bridge", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            bridge_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_client_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_notify_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-notify", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            notify_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_ask_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-ask", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            ask_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out


class TestMissingConfig:
    """Missing JID config must produce a clear SystemExit error."""

    def test_bridge_main_missing_jid(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-bridge"])
        # Clear env vars that might provide config
        monkeypatch.delenv("CLAUDE_XMPP_JID", raising=False)
        monkeypatch.delenv("CLAUDE_XMPP_RECIPIENT", raising=False)
        monkeypatch.delenv("CLAUDE_XMPP_CREDENTIALS", raising=False)
        # Ensure no config.toml is found by pointing to a non-existent directory
        monkeypatch.setattr(
            "claude_xmpp_bridge.config.CONFIG_FILE",
            __import__("pathlib").Path("/nonexistent/config.toml"),
        )
        with pytest.raises(SystemExit) as exc_info:
            bridge_main()
        # Should contain a helpful error message (not just exit code)
        msg = str(exc_info.value)
        assert "JID" in msg or "jid" in msg

    def test_notify_main_missing_jid(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-notify", "test message"])
        monkeypatch.delenv("CLAUDE_XMPP_JID", raising=False)
        monkeypatch.delenv("CLAUDE_XMPP_RECIPIENT", raising=False)
        monkeypatch.delenv("CLAUDE_XMPP_CREDENTIALS", raising=False)
        monkeypatch.setattr(
            "claude_xmpp_bridge.config.CONFIG_FILE",
            __import__("pathlib").Path("/nonexistent/config.toml"),
        )
        with pytest.raises(SystemExit) as exc_info:
            notify_main()
        msg = str(exc_info.value)
        assert "JID" in msg or "jid" in msg

    def test_ask_main_missing_jid(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-ask", "test message"])
        monkeypatch.delenv("CLAUDE_XMPP_JID", raising=False)
        monkeypatch.delenv("CLAUDE_XMPP_RECIPIENT", raising=False)
        monkeypatch.delenv("CLAUDE_XMPP_CREDENTIALS", raising=False)
        monkeypatch.setattr(
            "claude_xmpp_bridge.config.CONFIG_FILE",
            __import__("pathlib").Path("/nonexistent/config.toml"),
        )
        # Simulate no bridge running so ask_main falls through to direct XMPP path
        monkeypatch.setattr("claude_xmpp_bridge.client.send_to_bridge", lambda *a, **kw: None)
        with pytest.raises(SystemExit) as exc_info:
            ask_main()
        msg = str(exc_info.value)
        assert "JID" in msg or "jid" in msg


class TestClientMissingCommand:
    """Client without subcommand should exit non-zero."""

    def test_client_no_subcommand(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client"])
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 1


class TestNotifyMissingMessage:
    """Notify without message and with a tty should exit non-zero."""

    def test_notify_no_message_tty(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-notify"])
        # Simulate a tty (no piped input)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            notify_main()
        assert exc_info.value.code == 1


class TestClientSubcommands:
    """client_main subcommand routing — invalid JSON, socket unavailable."""

    def test_register_invalid_json_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "register", "not-json{{{"])
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 1
        assert "invalid JSON" in capsys.readouterr().err

    def test_response_invalid_json_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "response", "not-json{{{"])
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 1
        assert "invalid JSON" in capsys.readouterr().err

    def test_send_empty_message_exits_zero(self, monkeypatch, tmp_path):
        """send with empty stdin and no args should exit 0 silently."""
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "send"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "read", lambda: "   ")  # whitespace only
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 0

    def test_send_no_message_tty_exits_nonzero(self, monkeypatch, capsys):
        """send with no args and a tty stdin should exit non-zero."""
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "send"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 1

    def test_query_exits_nonzero_when_bridge_not_running(self, monkeypatch, tmp_path):
        """query with no running bridge should exit 1."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["claude-xmpp-client", "--socket-path", str(tmp_path / "no.sock"), "query", "sess-1"],
        )
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 1

    def test_get_context_prints_json(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "get-context", "sess-1"])
        monkeypatch.setattr(
            "claude_xmpp_bridge.client.send_to_bridge",
            lambda *a, **kw: {"ok": True, "session": {"session_id": "sess-1"}, "todos": [], "file_locks": []},
        )
        client_main()
        assert '"session_id": "sess-1"' in capsys.readouterr().out

    def test_add_todo_prints_json(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "add-todo", "sess-1", "hello"])
        monkeypatch.setattr(
            "claude_xmpp_bridge.client.send_to_bridge",
            lambda *a, **kw: {"ok": True, "todo": {"todo_id": "abc", "content": "hello"}, "version": 1},
        )
        client_main()
        assert '"todo_id": "abc"' in capsys.readouterr().out

    def test_list_locks_prints_json(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "list-locks"])
        monkeypatch.setattr(
            "claude_xmpp_bridge.client.send_to_bridge",
            lambda *a, **kw: {"ok": True, "locks": [{"filepath": "/tmp/a.py"}]},
        )
        client_main()
        assert '"filepath": "/tmp/a.py"' in capsys.readouterr().out

    def test_update_todo_unknown_session_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "update-todo", "sess-1", "todo-1", "--status", "done"])
        monkeypatch.setattr(
            "claude_xmpp_bridge.client.send_to_bridge",
            lambda *a, **kw: {"error": "unknown session_id: sess-1"},
        )
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 1
        assert "unknown session_id: sess-1" in capsys.readouterr().err

    def test_reply_last_prints_json(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "reply-last", "sess-1", "hello", "there"])
        monkeypatch.setattr(
            "claude_xmpp_bridge.client.send_to_bridge",
            lambda *a, **kw: {"ok": True, "to": "sess-2", "mode": "nudge"},
        )
        client_main()
        out = capsys.readouterr().out
        assert '"to": "sess-2"' in out
        assert '"mode": "nudge"' in out

    def test_relay_uses_bridge_session_id_from_env(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_SESSION_ID", "sess-env")
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "relay", "--to", "sess-target", "hello"])
        captured = {}

        def _send(req, *_args, **_kwargs):
            captured.update(req)
            return {"ok": True}

        monkeypatch.setattr("claude_xmpp_bridge.client.send_to_bridge", _send)
        client_main()
        assert captured["session_id"] == "sess-env"

    def test_broadcast_uses_bridge_session_id_from_env(self, monkeypatch, capsys):
        monkeypatch.setenv("BRIDGE_SESSION_ID", "sess-env")
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "broadcast", "hello"])
        captured = {}

        def _send(req, *_args, **_kwargs):
            captured.update(req)
            return {"ok": True, "delivered": 1}

        monkeypatch.setattr("claude_xmpp_bridge.client.send_to_bridge", _send)
        client_main()
        assert captured["session_id"] == "sess-env"
        assert "broadcast delivered to 1 session(s)" in capsys.readouterr().out


class TestAskMissingMessage:
    """ask without message and with a tty should exit non-zero."""

    def test_ask_no_message_tty(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-ask"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with pytest.raises(SystemExit) as exc_info:
            ask_main()
        assert exc_info.value.code == 1

    def test_ask_empty_stdin_exits_zero(self, monkeypatch):
        """ask with empty piped stdin should exit 0 silently."""
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-ask"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "read", lambda: "   ")
        with pytest.raises(SystemExit) as exc_info:
            ask_main()
        assert exc_info.value.code == 0
