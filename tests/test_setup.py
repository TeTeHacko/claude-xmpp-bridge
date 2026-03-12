"""Tests for claude_xmpp_bridge.setup — interactive setup wizard."""

from __future__ import annotations

import json
import shutil
import subprocess

from claude_xmpp_bridge import setup
from claude_xmpp_bridge.setup import (
    ALL_COMPONENTS,
    COMPONENT_BRIDGE,
    COMPONENT_HOOKS,
    COMPONENT_OPENCODE,
    COMPONENT_SANDBOX,
    HOOK_FILES_BRIDGE,
    HOOK_FILES_LOCAL,
    PLUGIN_MODE_NORMAL,
    PLUGIN_MODE_TITLE_ONLY,
    _ask_components,
    _confirm,
    _find_hooks_dir,
    _find_opencode_dir,
    _install_symlink,
    _resolve_plugin_source,
    _step_config,
    _step_credentials,
    _step_hooks,
    _step_opencode,
    _step_sandbox,
    _step_switches,
    _step_systemd,
    _uninstall_bridge,
    _uninstall_hooks,
    _uninstall_opencode,
    _uninstall_sandbox,
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
    def test_installs_all_hooks_as_symlinks_with_bridge(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        ok = _step_hooks(yes_mode=True, with_bridge=True)

        assert ok is True
        assert hooks_dir.is_dir()
        # All hook targets should be installed as symlinks
        for target in {**HOOK_FILES_LOCAL, **HOOK_FILES_BRIDGE}.values():
            dst = hooks_dir / target
            assert dst.is_symlink(), f"{target} must be a symlink"
            assert dst.is_file(), f"{target} symlink must point to existing file"

    def test_installs_only_title_hook_without_bridge(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        ok = _step_hooks(yes_mode=True, with_bridge=False)

        assert ok is True
        assert (hooks_dir / "session-start-title.sh").is_symlink()
        # Bridge-only hooks must NOT be installed
        for target in HOOK_FILES_BRIDGE.values():
            assert not (hooks_dir / target).exists(), f"unexpected {target}"

    def test_hook_symlinks_resolve_to_executable(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _step_hooks(yes_mode=True, with_bridge=True)

        for f in hooks_dir.iterdir():
            resolved = f.resolve()
            assert resolved.stat().st_mode & 0o100, f"{f.name} target is not executable"

    def test_replaces_plain_file_with_symlink(self, monkeypatch, tmp_path):
        """Upgrade path: existing plain file hooks are replaced with symlinks."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir(parents=True)
        old_hook = hooks_dir / "session-start-title.sh"
        old_hook.write_text("#!/bin/bash\n# old copy")
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        ok = _step_hooks(yes_mode=True, upgrade=True, with_bridge=True)

        assert ok is True
        assert old_hook.is_symlink(), "plain file must be replaced with symlink on upgrade"

    def test_merges_settings_json(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        settings_path.write_text('{"existing_key": "value"}')
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _step_hooks(yes_mode=True, with_bridge=True)

        data = json.loads(settings_path.read_text())
        assert "existing_key" in data
        assert "hooks" in data
        assert "SessionStart" in data["hooks"]

    def test_settings_json_with_bridge_false_has_sessionstart(self, monkeypatch, tmp_path):
        """Even without bridge, SessionStart hook should be in settings.json."""
        hooks_dir = tmp_path / "hooks"
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _step_hooks(yes_mode=True, with_bridge=False)

        data = json.loads(settings_path.read_text())
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

    def test_installs_unit_as_symlink(self, monkeypatch, tmp_path):
        systemd_dir = tmp_path / "systemd"
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/systemctl")

        ok = _step_systemd(yes_mode=True)

        assert ok is True
        dst = systemd_dir / "claude-xmpp-bridge.service"
        assert dst.is_symlink(), "systemd unit must be a symlink"
        assert dst.is_file(), "systemd unit symlink must point to existing file"

    def test_replaces_plain_file_with_symlink(self, monkeypatch, tmp_path):
        """Upgrade path: existing plain-file unit is replaced with symlink."""
        systemd_dir = tmp_path / "systemd"
        systemd_dir.mkdir(parents=True)
        old_unit = systemd_dir / "claude-xmpp-bridge.service"
        old_unit.write_text("[Unit]\nDescription=old copy\n")
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/systemctl")

        ok = _step_systemd(yes_mode=True, upgrade=True)

        assert ok is True
        assert old_unit.is_symlink(), "plain file must be replaced with symlink on upgrade"


# ---------------------------------------------------------------------------
# _find_opencode_dir
# ---------------------------------------------------------------------------


class TestFindOpencodeDir:
    def test_finds_source_tree(self):
        """Should find examples/opencode/ in the source tree."""
        result = _find_opencode_dir()
        assert result is not None
        assert (result / "plugins" / "xmpp-bridge.js").is_file()


# ---------------------------------------------------------------------------
# _step_opencode
# ---------------------------------------------------------------------------


class TestStepOpencode:
    def test_installs_plugin_as_symlink(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _step_opencode(yes_mode=True)

        assert ok is True
        dst = plugins_dir / "xmpp-bridge.js"
        assert dst.is_symlink(), "plugin must be installed as a symlink"
        assert dst.is_file(), "symlink must point to an existing file"

    def test_installed_plugin_has_source_field(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        _step_opencode(yes_mode=True)

        plugin_text = (plugins_dir / "xmpp-bridge.js").read_text()
        assert 'source:     "opencode"' in plugin_text

    def test_title_only_does_not_modify_plugin(self, monkeypatch, tmp_path):
        """Title-only mode installs the same (unmodified) plugin file via symlink."""
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _step_opencode(yes_mode=True, plugin_mode=PLUGIN_MODE_TITLE_ONLY)

        assert ok is True
        dst = plugins_dir / "xmpp-bridge.js"
        assert dst.is_symlink()
        # Plugin keeps default BRIDGE_MODE = "auto" — title-only is set via env
        plugin_text = dst.read_text()
        assert 'const BRIDGE_MODE = process.env.XMPP_BRIDGE_MODE ?? "auto"' in plugin_text

    def test_normal_mode_installs_symlink(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _step_opencode(yes_mode=True, plugin_mode=PLUGIN_MODE_NORMAL)

        assert ok is True
        dst = plugins_dir / "xmpp-bridge.js"
        assert dst.is_symlink()

    def test_symlink_target_matches_resolve_plugin_source(self, monkeypatch, tmp_path):
        """Symlink must point to the resolved canonical path."""
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        _step_opencode(yes_mode=True)

        dst = plugins_dir / "xmpp-bridge.js"
        opencode_dir = _find_opencode_dir()
        assert opencode_dir is not None
        expected = _resolve_plugin_source(opencode_dir)
        assert dst.resolve() == expected

    def test_replaces_plain_file_with_symlink(self, monkeypatch, tmp_path):
        """Upgrade path: existing plain file is replaced with a symlink."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir(parents=True)
        old_file = plugins_dir / "xmpp-bridge.js"
        old_file.write_text("old plain copy")
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _step_opencode(yes_mode=True, upgrade=True)

        assert ok is True
        assert old_file.is_symlink(), "plain file must be replaced with symlink on upgrade"

    def test_removes_legacy_plugin_on_install(self, monkeypatch, tmp_path):
        """Legacy plugin copy must be removed during install/upgrade."""
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        legacy_plugin.parent.mkdir(parents=True, exist_ok=True)
        legacy_plugin.write_text("old plugin")
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _step_opencode(yes_mode=True)

        assert ok is True
        assert not legacy_plugin.exists(), "legacy plugin copy must be removed"

    def test_merges_opencode_json(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _step_opencode(yes_mode=True)

        assert ok is True
        assert settings_path.is_file()
        data = json.loads(settings_path.read_text())
        assert "permission" in data

    def test_preserves_existing_keys(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        settings_path = tmp_path / "opencode.json"
        settings_path.write_text('{"existing_key": "value"}')
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

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


# ---------------------------------------------------------------------------
# _install_symlink
# ---------------------------------------------------------------------------


class TestInstallSymlink:
    def test_creates_symlink_to_source(self, tmp_path):
        src = tmp_path / "source" / "file.txt"
        src.parent.mkdir()
        src.write_text("content")
        dst = tmp_path / "dest" / "file.txt"

        changed = _install_symlink(src, dst)

        assert changed is True
        assert dst.is_symlink()
        assert dst.resolve() == src.resolve()
        assert dst.read_text() == "content"

    def test_returns_false_when_already_correct(self, tmp_path):
        src = tmp_path / "source" / "file.txt"
        src.parent.mkdir()
        src.write_text("content")
        dst = tmp_path / "dest" / "file.txt"
        dst.parent.mkdir()
        dst.symlink_to(src.resolve())

        changed = _install_symlink(src, dst)

        assert changed is False

    def test_replaces_plain_file(self, tmp_path):
        src = tmp_path / "source" / "file.txt"
        src.parent.mkdir()
        src.write_text("new content")
        dst = tmp_path / "dest" / "file.txt"
        dst.parent.mkdir()
        dst.write_text("old plain copy")

        changed = _install_symlink(src, dst)

        assert changed is True
        assert dst.is_symlink()
        assert dst.read_text() == "new content"

    def test_replaces_wrong_symlink(self, tmp_path):
        old_target = tmp_path / "old" / "file.txt"
        old_target.parent.mkdir()
        old_target.write_text("old")
        new_src = tmp_path / "new" / "file.txt"
        new_src.parent.mkdir()
        new_src.write_text("new")
        dst = tmp_path / "dest" / "file.txt"
        dst.parent.mkdir()
        dst.symlink_to(old_target.resolve())

        changed = _install_symlink(new_src, dst)

        assert changed is True
        assert dst.resolve() == new_src.resolve()

    def test_replaces_dangling_symlink(self, tmp_path):
        src = tmp_path / "source" / "file.txt"
        src.parent.mkdir()
        src.write_text("content")
        dst = tmp_path / "dest" / "file.txt"
        dst.parent.mkdir()
        dst.symlink_to(tmp_path / "nonexistent")

        changed = _install_symlink(src, dst)

        assert changed is True
        assert dst.is_symlink()
        assert dst.resolve() == src.resolve()

    def test_make_executable(self, tmp_path):
        src = tmp_path / "script.sh"
        src.write_text("#!/bin/bash")
        src.chmod(0o644)  # not executable
        dst = tmp_path / "dest" / "script.sh"

        _install_symlink(src, dst, make_executable=True)

        assert src.stat().st_mode & 0o100, "source must be made executable"


# ---------------------------------------------------------------------------
# _step_sandbox
# ---------------------------------------------------------------------------


class TestStepSandbox:
    def test_installs_sandbox_as_symlink(self, monkeypatch, tmp_path):
        sandbox_dst = tmp_path / "sandbox"
        completion_dst = tmp_path / "completion" / "sandbox"
        monkeypatch.setattr(setup, "SANDBOX_DST", sandbox_dst)
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", completion_dst)

        ok = _step_sandbox(yes_mode=True)

        assert ok is True
        assert sandbox_dst.is_symlink(), "sandbox must be a symlink"
        assert sandbox_dst.is_file(), "sandbox symlink must point to existing file"

    def test_installs_bash_completion_as_symlink(self, monkeypatch, tmp_path):
        sandbox_dst = tmp_path / "sandbox"
        completion_dst = tmp_path / "completion" / "sandbox"
        monkeypatch.setattr(setup, "SANDBOX_DST", sandbox_dst)
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", completion_dst)

        ok = _step_sandbox(yes_mode=True)

        assert ok is True
        assert completion_dst.is_symlink(), "bash completion must be a symlink"

    def test_replaces_plain_file_with_symlink(self, monkeypatch, tmp_path):
        """Upgrade path: existing plain file is replaced with a symlink."""
        sandbox_dst = tmp_path / "sandbox"
        sandbox_dst.write_text("#!/bin/bash\n# old copy")
        completion_dst = tmp_path / "completion" / "sandbox"
        monkeypatch.setattr(setup, "SANDBOX_DST", sandbox_dst)
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", completion_dst)

        ok = _step_sandbox(yes_mode=True, upgrade=True)

        assert ok is True
        assert sandbox_dst.is_symlink(), "plain file must be replaced with symlink on upgrade"


# ---------------------------------------------------------------------------
# _ask_components
# ---------------------------------------------------------------------------


class TestAskComponents:
    def test_yes_mode_returns_all(self):
        result = _ask_components(yes_mode=True)
        assert result == set(ALL_COMPONENTS)

    def test_empty_input_returns_all_by_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        result = _ask_components(yes_mode=False, default_all=True)
        assert result == set(ALL_COMPONENTS)

    def test_toggle_deselects_component(self, monkeypatch):
        # First input toggles component 4 (bridge-daemon) off, second confirms
        calls = iter(["4", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(calls))
        result = _ask_components(yes_mode=False, default_all=True)
        assert COMPONENT_BRIDGE not in result
        assert COMPONENT_SANDBOX in result

    def test_toggle_twice_restores(self, monkeypatch):
        # Toggle bridge off then on again, then confirm
        calls = iter(["4", "4", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(calls))
        result = _ask_components(yes_mode=False, default_all=True)
        assert COMPONENT_BRIDGE in result

    def test_multiple_toggles_space_separated(self, monkeypatch):
        # Toggle sandbox (1) and opencode (3) off
        calls = iter(["1 3", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(calls))
        result = _ask_components(yes_mode=False, default_all=True)
        assert COMPONENT_SANDBOX not in result
        assert COMPONENT_OPENCODE not in result
        assert COMPONENT_HOOKS in result
        assert COMPONENT_BRIDGE in result

    def test_default_all_false_starts_empty(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        result = _ask_components(yes_mode=False, default_all=False)
        assert result == set()


class TestReleaseGuards:
    def test_plugin_changes_require_package_version_bump(self):
        if shutil.which("git") is None:
            return

        repo_root = setup.Path(__file__).resolve().parent.parent
        plugin_path = "examples/opencode/plugins/xmpp-bridge.js"
        version_file = "src/claude_xmpp_bridge/__init__.py"

        diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", plugin_path],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if diff.returncode != 0 or plugin_path not in diff.stdout.splitlines():
            return

        head_version = subprocess.run(
            ["git", "show", f"HEAD:{version_file}"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if head_version.returncode != 0:
            return

        current_version = setup.__version__
        previous_version = None
        for line in head_version.stdout.splitlines():
            if line.startswith("__version__ = "):
                previous_version = line.split('"')[1]
                break

        assert previous_version is not None
        assert current_version != previous_version, (
            "OpenCode plugin changed but package version did not change. "
            "Bump pyproject.toml and src/claude_xmpp_bridge/__init__.py so pipx upgrade will reinstall the package."
        )


# ---------------------------------------------------------------------------
# _uninstall_sandbox
# ---------------------------------------------------------------------------


class TestUninstallSandbox:
    def test_removes_installed_files(self, monkeypatch, tmp_path):
        sandbox_dst = tmp_path / "sandbox"
        completion_dst = tmp_path / "sandbox.bash-completion"
        sandbox_dst.write_text("#!/bin/bash")
        completion_dst.write_text("# completion")
        monkeypatch.setattr(setup, "SANDBOX_DST", sandbox_dst)
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", completion_dst)

        ok = _uninstall_sandbox(yes_mode=True)

        assert ok is True
        assert not sandbox_dst.exists()
        assert not completion_dst.exists()

    def test_removes_symlinks(self, monkeypatch, tmp_path):
        """Uninstall must also handle symlinks from the new install model."""
        target = tmp_path / "source" / "sandbox"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/bash")
        sandbox_dst = tmp_path / "sandbox"
        sandbox_dst.symlink_to(target)
        completion_dst = tmp_path / "completion"
        monkeypatch.setattr(setup, "SANDBOX_DST", sandbox_dst)
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", completion_dst)

        ok = _uninstall_sandbox(yes_mode=True)

        assert ok is True
        assert not sandbox_dst.exists()
        assert not sandbox_dst.is_symlink()
        # Source file must remain untouched
        assert target.is_file()

    def test_reports_not_found(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(setup, "SANDBOX_DST", tmp_path / "sandbox")
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", tmp_path / "completion")

        ok = _uninstall_sandbox(yes_mode=True)

        assert ok is True
        out = capsys.readouterr().out
        assert "Not found" in out

    def test_skips_when_declined(self, monkeypatch, tmp_path):
        sandbox_dst = tmp_path / "sandbox"
        sandbox_dst.write_text("#!/bin/bash")
        monkeypatch.setattr(setup, "SANDBOX_DST", sandbox_dst)
        monkeypatch.setattr(setup, "SANDBOX_COMPLETION_DST", tmp_path / "completion")
        monkeypatch.setattr("builtins.input", lambda _: "n")

        _uninstall_sandbox(yes_mode=False)

        assert sandbox_dst.exists()


# ---------------------------------------------------------------------------
# _uninstall_hooks
# ---------------------------------------------------------------------------


class TestUninstallHooks:
    def _setup_hooks(self, tmp_path: object, monkeypatch: object) -> object:  # type: ignore[override]
        hooks_dir = tmp_path / "hooks"  # type: ignore[union-attr]
        hooks_dir.mkdir()
        settings_path = tmp_path / "settings.json"  # type: ignore[union-attr]
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)  # type: ignore[attr-defined]
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)  # type: ignore[attr-defined]
        return hooks_dir, settings_path  # type: ignore[return-value]

    def test_removes_hook_files(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)
        # Create some hook files
        for name in ["session-start-title.sh", "notification.sh", "permission-ask-xmpp.sh"]:
            (hooks_dir / name).write_text("#!/bin/bash")

        ok = _uninstall_hooks(yes_mode=True)

        assert ok is True
        assert not (hooks_dir / "session-start-title.sh").exists()
        assert not (hooks_dir / "notification.sh").exists()

    def test_removes_hook_symlinks(self, monkeypatch, tmp_path):
        """Uninstall must handle symlinks from the new install model."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)
        # Create symlinks instead of plain files
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        for name in ["session-start-title.sh", "notification.sh"]:
            target = source_dir / name
            target.write_text("#!/bin/bash")
            (hooks_dir / name).symlink_to(target)

        ok = _uninstall_hooks(yes_mode=True)

        assert ok is True
        assert not (hooks_dir / "session-start-title.sh").exists()
        assert not (hooks_dir / "session-start-title.sh").is_symlink()
        # Source files must remain untouched
        assert (source_dir / "session-start-title.sh").is_file()

    def test_removes_hook_events_from_settings(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "other_key": "value",
                    "hooks": {
                        "SessionStart": [{"type": "command", "command": "session-start-title.sh"}],
                        "TaskCompleted": [{"type": "command", "command": "task-completed.sh"}],
                    },
                }
            )
        )
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _uninstall_hooks(yes_mode=True)

        data = json.loads(settings_path.read_text())
        assert "other_key" in data
        assert "SessionStart" not in data.get("hooks", {})
        assert "TaskCompleted" not in data.get("hooks", {})

    def test_preserves_non_managed_hook_events(self, monkeypatch, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [{"type": "command", "command": "session-start-title.sh"}],
                        "CustomEvent": [{"type": "command", "command": "my-custom-hook.sh"}],
                    },
                }
            )
        )
        monkeypatch.setattr(setup, "HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(setup, "CLAUDE_SETTINGS", settings_path)

        _uninstall_hooks(yes_mode=True)

        data = json.loads(settings_path.read_text())
        # CustomEvent is not in MANAGED_HOOK_EVENTS, must be preserved
        assert "CustomEvent" in data.get("hooks", {})


# ---------------------------------------------------------------------------
# _uninstall_opencode
# ---------------------------------------------------------------------------


class TestUninstallOpencode:
    def test_removes_plugin_file(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        plugin_file = plugins_dir / "xmpp-bridge.js"
        plugin_file.write_text("export const XmppBridgePlugin = () => {}")
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        settings_path = tmp_path / "opencode.json"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _uninstall_opencode(yes_mode=True)

        assert ok is True
        assert not plugin_file.exists()

    def test_removes_permission_key_from_opencode_json(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        settings_path = tmp_path / "opencode.json"
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        settings_path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "permission": {"bash": "ask", "edit": "ask"},
                }
            )
        )
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        _uninstall_opencode(yes_mode=True)

        data = json.loads(settings_path.read_text())
        assert "permission" not in data
        assert data["theme"] == "dark"

    def test_no_error_when_settings_missing(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", tmp_path / "opencode.json")
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", tmp_path / "legacy" / "plugins" / "xmpp-bridge.js")

        ok = _uninstall_opencode(yes_mode=True)

        assert ok is True

    def test_removes_legacy_plugin_file(self, monkeypatch, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        legacy_plugin.parent.mkdir(parents=True, exist_ok=True)
        legacy_plugin.write_text("old plugin")
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", tmp_path / "opencode.json")
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _uninstall_opencode(yes_mode=True)

        assert ok is True
        assert not legacy_plugin.exists()

    def test_removes_plugin_symlink(self, monkeypatch, tmp_path):
        """Uninstall must remove symlink correctly."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        # Create a symlink pointing to some file
        target = tmp_path / "source" / "xmpp-bridge.js"
        target.parent.mkdir(parents=True)
        target.write_text("plugin content")
        link = plugins_dir / "xmpp-bridge.js"
        link.symlink_to(target)
        legacy_plugin = tmp_path / "legacy" / "plugins" / "xmpp-bridge.js"
        settings_path = tmp_path / "opencode.json"
        monkeypatch.setattr(setup, "OPENCODE_PLUGINS_DIR", plugins_dir)
        monkeypatch.setattr(setup, "OPENCODE_SETTINGS", settings_path)
        monkeypatch.setattr(setup, "LEGACY_OPENCODE_PLUGIN_DST", legacy_plugin)

        ok = _uninstall_opencode(yes_mode=True)

        assert ok is True
        assert not link.exists()
        assert not link.is_symlink()
        # Source file must remain untouched
        assert target.is_file()


# ---------------------------------------------------------------------------
# _uninstall_bridge
# ---------------------------------------------------------------------------


class TestUninstallBridge:
    def test_removes_systemd_unit(self, monkeypatch, tmp_path):
        systemd_dir = tmp_path / "systemd"
        systemd_dir.mkdir()
        unit = systemd_dir / "claude-xmpp-bridge.service"
        unit.write_text("[Unit]\nDescription=test\n")
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr("shutil.which", lambda cmd: None)  # no systemctl

        ok = _uninstall_bridge(yes_mode=True)

        assert ok is True
        assert not unit.exists()

    def test_removes_systemd_unit_symlink(self, monkeypatch, tmp_path):
        """Uninstall must handle symlinks from the new install model."""
        systemd_dir = tmp_path / "systemd"
        systemd_dir.mkdir()
        target = tmp_path / "source" / "claude-xmpp-bridge.service"
        target.parent.mkdir()
        target.write_text("[Unit]\nDescription=test\n")
        unit = systemd_dir / "claude-xmpp-bridge.service"
        unit.symlink_to(target)
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        ok = _uninstall_bridge(yes_mode=True)

        assert ok is True
        assert not unit.exists()
        assert not unit.is_symlink()
        # Source file must remain untouched
        assert target.is_file()

    def test_purge_removes_config_dir(self, monkeypatch, tmp_path):
        systemd_dir = tmp_path / "systemd"
        systemd_dir.mkdir()
        config_dir = tmp_path / "claude-xmpp-bridge"
        config_dir.mkdir()
        (config_dir / "credentials").write_text("pw")
        switches_dir = tmp_path / "xmpp-notify"
        switches_dir.mkdir()
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr(setup, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(setup, "SWITCHES_DIR", switches_dir)
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        ok = _uninstall_bridge(yes_mode=True, purge=True)

        assert ok is True
        assert not config_dir.exists()

    def test_no_purge_keeps_config_dir(self, monkeypatch, tmp_path):
        systemd_dir = tmp_path / "systemd"
        systemd_dir.mkdir()
        config_dir = tmp_path / "claude-xmpp-bridge"
        config_dir.mkdir()
        (config_dir / "credentials").write_text("pw")
        monkeypatch.setattr(setup, "SYSTEMD_DIR", systemd_dir)
        monkeypatch.setattr(setup, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(setup, "SWITCHES_DIR", tmp_path / "xmpp-notify")
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        ok = _uninstall_bridge(yes_mode=True, purge=False)

        assert ok is True
        assert config_dir.exists()


# ---------------------------------------------------------------------------
# bridge ping command
# ---------------------------------------------------------------------------


class TestBridgePing:
    def test_ping_returns_ok(self, tmp_path):
        """Bridge _handle_request must respond to ping with {ok: True}."""
        import asyncio

        from claude_xmpp_bridge.bridge import XMPPBridge
        from claude_xmpp_bridge.config import Config

        cfg = Config(
            jid="bot@example.com",
            password="pw",
            recipient="user@example.com",
            socket_path=tmp_path / "bridge.sock",
            db_path=tmp_path / "bridge.db",
            messages_file=None,
            socket_token=None,
            force_starttls=True,
            source_icons={},
            audit_log="journald",
        )
        bridge = XMPPBridge(cfg)
        result = asyncio.run(bridge._handle_request({"cmd": "ping"}))
        assert result == {"ok": True}
        bridge.registry.close()
