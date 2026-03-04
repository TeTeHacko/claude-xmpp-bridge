"""Tests for claude_xmpp_bridge.setup — interactive setup wizard."""

from __future__ import annotations

import json

from claude_xmpp_bridge import setup
from claude_xmpp_bridge.setup import (
    _confirm,
    _find_hooks_dir,
    _find_opencode_dir,
    _step_config,
    _step_credentials,
    _step_hooks,
    _step_opencode,
    _step_switches,
    _step_systemd,
)

# ---------------------------------------------------------------------------
# _confirm
# ---------------------------------------------------------------------------


class TestConfirm:
    def test_yes_mode_returns_default(self):
        assert _confirm("Test?", default=True, yes_mode=True) is True
        assert _confirm("Test?", default=False, yes_mode=True) is False

    def test_empty_input_returns_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _confirm("Test?", default=True) is True
        assert _confirm("Test?", default=False) is False

    def test_yes_input(self, monkeypatch):
        for answer in ("y", "Y", "yes", "YES"):
            monkeypatch.setattr("builtins.input", lambda _, a=answer: a)
            assert _confirm("Test?", default=False) is True

    def test_no_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert _confirm("Test?", default=True) is False


# ---------------------------------------------------------------------------
# _find_hooks_dir
# ---------------------------------------------------------------------------


class TestFindHooksDir:
    def test_finds_source_tree(self):
        """Should find examples/hooks/ in the source tree."""
        result = _find_hooks_dir()
        assert result is not None
        assert (result / "session-start-title.sh").is_file()


# ---------------------------------------------------------------------------
# _step_credentials
# ---------------------------------------------------------------------------


class TestStepCredentials:
    def test_creates_credentials_file(self, monkeypatch, tmp_path):
        cred_dir = tmp_path / "config"
        cred_file = cred_dir / "credentials"
        monkeypatch.setattr(setup, "CONFIG_DIR", cred_dir)
        monkeypatch.setattr("getpass.getpass", lambda _: "test-pw-123")

        ok = _step_credentials(yes_mode=False)

        assert ok is True
        assert cred_file.is_file()
        assert cred_file.read_text() == "test-pw-123\n"
        assert (cred_file.stat().st_mode & 0o777) == 0o600

    def test_skips_in_yes_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "CONFIG_DIR", tmp_path / "config")

        ok = _step_credentials(yes_mode=True)

        assert ok is True
        assert not (tmp_path / "config" / "credentials").exists()

    def test_rejects_empty_password(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr("getpass.getpass", lambda _: "")

        ok = _step_credentials(yes_mode=False)

        assert ok is False

    def test_keeps_existing_if_not_confirmed(self, monkeypatch, tmp_path):
        cred_dir = tmp_path / "config"
        cred_dir.mkdir()
        cred_file = cred_dir / "credentials"
        cred_file.write_text("old-password")
        monkeypatch.setattr(setup, "CONFIG_DIR", cred_dir)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        ok = _step_credentials(yes_mode=False)

        assert ok is True
        assert cred_file.read_text() == "old-password"


# ---------------------------------------------------------------------------
# _step_config
# ---------------------------------------------------------------------------


class TestStepConfig:
    def test_creates_config_file(self, monkeypatch, tmp_path):
        config_dir = tmp_path / "config"
        config_file = config_dir / "config.toml"
        monkeypatch.setattr(setup, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(setup, "CONFIG_FILE", config_file)

        inputs = iter(["bot@example.com", "user@example.com"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        ok = _step_config(yes_mode=False)

        assert ok is True
        assert config_file.is_file()
        content = config_file.read_text()
        assert 'jid = "bot@example.com"' in content
        assert 'recipient = "user@example.com"' in content

    def test_skips_in_yes_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_path / "config" / "config.toml")

        ok = _step_config(yes_mode=True)

        assert ok is True
        assert not (tmp_path / "config" / "config.toml").exists()

    def test_rejects_invalid_jid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_path / "config" / "config.toml")
        monkeypatch.setattr("builtins.input", lambda _: "nope")

        ok = _step_config(yes_mode=False)

        assert ok is False

    def test_rejects_invalid_recipient(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_path / "config" / "config.toml")

        inputs = iter(["bot@example.com", "nope"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        ok = _step_config(yes_mode=False)

        assert ok is False


# ---------------------------------------------------------------------------
# _step_hooks
# ---------------------------------------------------------------------------


class TestStepHooks:
    def test_installs_hooks(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        ok = _step_hooks(yes_mode=True)

        assert ok is True
        assert hooks_dir.is_dir()
        assert (hooks_dir / "session-start-title.sh").is_file()
        assert (hooks_dir / "notification.sh").is_file()
        assert (hooks_dir / "permission-ask-xmpp.sh").is_file()

    def test_scripts_are_executable(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _step_hooks(yes_mode=True)

        for f in hooks_dir.iterdir():
            assert f.stat().st_mode & 0o100, f"{f.name} is not executable"

    def test_merges_settings_json(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        settings_path.write_text('{"existing_key": "value"}')
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _step_hooks(yes_mode=True)

        data = json.loads(settings_path.read_text())
        assert "existing_key" in data
        assert "hooks" in data
        assert "SessionStart" in data["hooks"]

    def test_skips_if_declined(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        ok = _step_hooks(yes_mode=False)

        assert ok is True
        assert not hooks_dir.exists()


# ---------------------------------------------------------------------------
# _step_switches
# ---------------------------------------------------------------------------


class TestStepSwitches:
    def test_enables_switches_in_yes_mode(self, monkeypatch, tmp_path):
        switches_dir = tmp_path / "switches"
        monkeypatch.setattr(setup, "SWITCHES_DIR", switches_dir)

        ok = _step_switches(yes_mode=True)

        assert ok is True
        assert (switches_dir / "notify-enabled").is_file()
        assert (switches_dir / "ask-enabled").is_file()

    def test_skips_existing_switches(self, monkeypatch, tmp_path, capsys):
        switches_dir = tmp_path / "switches"
        switches_dir.mkdir()
        (switches_dir / "notify-enabled").touch()
        monkeypatch.setattr(setup, "SWITCHES_DIR", switches_dir)

        _step_switches(yes_mode=True)

        captured = capsys.readouterr()
        assert "already enabled" in captured.out


# ---------------------------------------------------------------------------
# _step_systemd
# ---------------------------------------------------------------------------


class TestStepSystemd:
    def test_skips_without_systemctl(self, monkeypatch, capsys):
        monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "systemctl" else "/usr/bin/" + cmd)

        ok = _step_systemd(yes_mode=True)

        assert ok is True
        captured = capsys.readouterr()
        assert "not found" in captured.out

    def test_installs_unit(self, monkeypatch, tmp_path):
        systemd_dir = tmp_path / "systemd"
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/systemctl")

        ok = _step_systemd(yes_mode=True)

        assert ok is True
        assert (systemd_dir / "claude-xmpp-bridge.service").is_file()


# ---------------------------------------------------------------------------
# _find_opencode_dir
# ---------------------------------------------------------------------------


class TestFindOpencodeDir:
    def test_finds_source_tree(self):
        """Should find examples/opencode/ in the source tree."""
        result = _find_opencode_dir()
        assert result is not None
        assert (result / "plugins" / "xmpp-bridge.js").is_file()

    def test_plugin_contains_source_field(self):
        """Plugin file must contain source: \"opencode\" in register payloads."""
        result = _find_opencode_dir()
        assert result is not None
        plugin_text = (result / "plugins" / "xmpp-bridge.js").read_text()
        assert 'source:     "opencode"' in plugin_text


# ---------------------------------------------------------------------------
# _step_opencode
# ---------------------------------------------------------------------------


class TestStepOpencode:
    def test_installs_plugin(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)

        ok = _step_opencode(yes_mode=True)

        assert ok is True
        assert (plugins_dir / "xmpp-bridge.js").is_file()

    def test_installed_plugin_has_source_field(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)

        _step_opencode(yes_mode=True)

        plugin_text = (plugins_dir / "xmpp-bridge.js").read_text()
        assert 'source:     "opencode"' in plugin_text

    def test_merges_opencode_json(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)

        ok = _step_opencode(yes_mode=True)

        assert ok is True
        assert settings_path.is_file()
        data = json.loads(settings_path.read_text())
        assert "permission" in data

    def test_preserves_existing_keys(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        settings_path.write_text('{"existing_key": "value"}')
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)

        _step_opencode(yes_mode=True)

        data = json.loads(settings_path.read_text())
        assert "existing_key" in data
        assert "permission" in data

    def test_skips_if_declined(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        ok = _step_opencode(yes_mode=False)

        assert ok is True
        assert not plugins_dir.exists()

    def test_skips_when_source_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "_find_opencode_dir", lambda: None)
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)

        ok = _step_opencode(yes_mode=True)

        assert ok is True
        assert not plugins_dir.exists()
