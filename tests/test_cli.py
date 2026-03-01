"""Tests for cli module — argument parsing and entry points."""

from __future__ import annotations

import sys

import pytest

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
        assert "0.1.0" in captured.out

    def test_client_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-client", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            client_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "0.1.0" in captured.out

    def test_notify_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-notify", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            notify_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "0.1.0" in captured.out

    def test_ask_main_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["claude-xmpp-ask", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            ask_main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "0.1.0" in captured.out


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
