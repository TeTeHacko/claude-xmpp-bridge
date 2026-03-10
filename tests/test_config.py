"""Tests for claude_xmpp_bridge.config — layered configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_xmpp_bridge import config
from claude_xmpp_bridge.config import (
    Config,
    NotifyConfig,
    _check_permissions,
    _read_password,
    _read_toml,
    _resolve_credentials,
    _toml_str,
    load_config,
    load_notify_config,
    validate_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_ENV_VARS = (
    "CLAUDE_XMPP_JID",
    "CLAUDE_XMPP_RECIPIENT",
    "CLAUDE_XMPP_CREDENTIALS",
    "CLAUDE_XMPP_SOCKET",
    "CLAUDE_XMPP_DB",
    "CLAUDE_XMPP_MESSAGES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove all CLAUDE_XMPP_* env vars so tests start from a clean slate."""
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _write_toml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# 1. Priority: CLI > env > TOML > defaults
# ---------------------------------------------------------------------------


class TestPriorityCLIOverEnv:
    """CLI arguments must override environment variables."""

    def test_cli_jid_overrides_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setenv("CLAUDE_XMPP_JID", "env@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        # Point CONFIG_FILE to non-existent path so TOML is empty
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        cfg = load_config(
            cli_jid="cli@example.com",
            cli_credentials=str(credentials_file),
        )
        assert cfg.jid == "cli@example.com"

    def test_cli_recipient_overrides_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "env-rcpt@example.com")
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        cfg = load_config(
            cli_recipient="cli-rcpt@example.com",
            cli_credentials=str(credentials_file),
        )
        assert cfg.recipient == "cli-rcpt@example.com"

    def test_cli_socket_path_overrides_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_SOCKET", "/env/socket.sock")
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        cfg = load_config(
            cli_socket_path="/cli/socket.sock",
            cli_credentials=str(credentials_file),
        )
        assert cfg.socket_path == Path("/cli/socket.sock")

    def test_cli_db_path_overrides_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_DB", "/env/bridge.db")
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        cfg = load_config(
            cli_db_path="/cli/bridge.db",
            cli_credentials=str(credentials_file),
        )
        assert cfg.db_path == Path("/cli/bridge.db")

    def test_cli_credentials_overrides_env(self, monkeypatch, credentials_file, tmp_path):
        env_cred = tmp_path / "env-cred"
        env_cred.write_text("env-password")
        env_cred.chmod(0o600)

        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_CREDENTIALS", str(env_cred))
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.password == "test-password"  # from credentials_file fixture

    def test_cli_messages_overrides_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_MESSAGES", "/env/messages.txt")
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        cfg = load_config(
            cli_messages="/cli/messages.txt",
            cli_credentials=str(credentials_file),
        )
        assert cfg.messages_file == Path("/cli/messages.txt")


class TestPriorityEnvOverTOML:
    """Environment variables must override TOML config."""

    def test_env_jid_overrides_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "toml@example.com"\nrecipient = "rcpt@example.com"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_JID", "env@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.jid == "env@example.com"

    def test_env_recipient_overrides_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "bot@example.com"\nrecipient = "toml-rcpt@example.com"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "env-rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.recipient == "env-rcpt@example.com"

    def test_env_socket_overrides_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            ('jid = "bot@example.com"\nrecipient = "rcpt@example.com"\nsocket_path = "/toml/socket.sock"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_SOCKET", "/env/socket.sock")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.socket_path == Path("/env/socket.sock")

    def test_env_db_overrides_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            ('jid = "bot@example.com"\nrecipient = "rcpt@example.com"\ndb_path = "/toml/bridge.db"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_DB", "/env/bridge.db")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.db_path == Path("/env/bridge.db")

    def test_env_credentials_overrides_toml(self, monkeypatch, tmp_path):
        env_cred = tmp_path / "env-cred"
        env_cred.write_text("env-password")
        env_cred.chmod(0o600)

        toml_cred = tmp_path / "toml-cred"
        toml_cred.write_text("toml-password")
        toml_cred.chmod(0o600)

        toml_file = _write_toml(
            tmp_path / "config.toml",
            (f'jid = "bot@example.com"\nrecipient = "rcpt@example.com"\ncredentials = "{toml_cred}"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_CREDENTIALS", str(env_cred))

        cfg = load_config()
        assert cfg.password == "env-password"


class TestPriorityTOMLOverDefaults:
    """TOML values must override built-in defaults."""

    def test_toml_socket_overrides_default(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            ('jid = "bot@example.com"\nrecipient = "rcpt@example.com"\nsocket_path = "/toml/my.sock"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.socket_path == Path("/toml/my.sock")

    def test_toml_db_overrides_default(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            ('jid = "bot@example.com"\nrecipient = "rcpt@example.com"\ndb_path = "/toml/my.db"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.db_path == Path("/toml/my.db")

    def test_toml_jid_used_when_no_cli_or_env(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "toml@example.com"\nrecipient = "rcpt@example.com"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.jid == "toml@example.com"
        assert cfg.recipient == "rcpt@example.com"


class TestFullPriorityChain:
    """Verify the full 3-layer override chain in a single test."""

    def test_cli_beats_env_beats_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            ('jid = "toml@example.com"\nrecipient = "toml-rcpt@example.com"\nsocket_path = "/toml/sock"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_JID", "env@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "env-rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_SOCKET", "/env/sock")

        cfg = load_config(
            cli_jid="cli@example.com",
            cli_recipient="cli-rcpt@example.com",
            cli_socket_path="/cli/sock",
            cli_credentials=str(credentials_file),
        )
        assert cfg.jid == "cli@example.com"
        assert cfg.recipient == "cli-rcpt@example.com"
        assert cfg.socket_path == Path("/cli/sock")


# ---------------------------------------------------------------------------
# 2. Missing credentials file -> FileNotFoundError
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    def test_raises_when_no_credentials_exist(self, monkeypatch, tmp_path):
        # Point default locations to non-existent paths
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "no-config")
        monkeypatch.setattr(config, "LEGACY_CREDENTIALS_FILE", tmp_path / "no-legacy" / "credentials")

        with pytest.raises(FileNotFoundError, match="Credentials file not found"):
            _resolve_credentials(None, None, None)

    def test_raises_with_helpful_message(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "no-config")
        monkeypatch.setattr(config, "LEGACY_CREDENTIALS_FILE", tmp_path / "no-legacy" / "credentials")

        with pytest.raises(FileNotFoundError, match="chmod 600"):
            _resolve_credentials(None, None, None)


# ---------------------------------------------------------------------------
# 3. Tilde expansion in paths
# ---------------------------------------------------------------------------


class TestTildeExpansion:
    def test_credentials_path_tilde_expanded(self):
        resolved = _resolve_credentials("~/my-creds", None, None)
        assert "~" not in str(resolved)
        assert resolved == Path.home() / "my-creds"

    def test_socket_path_tilde_expanded(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(
            cli_socket_path="~/my.sock",
            cli_credentials=str(credentials_file),
        )
        assert "~" not in str(cfg.socket_path)
        assert cfg.socket_path == Path.home() / "my.sock"

    def test_db_path_tilde_expanded(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(
            cli_db_path="~/my.db",
            cli_credentials=str(credentials_file),
        )
        assert "~" not in str(cfg.db_path)
        assert cfg.db_path == Path.home() / "my.db"

    def test_messages_file_tilde_expanded(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(
            cli_messages="~/msgs.txt",
            cli_credentials=str(credentials_file),
        )
        assert cfg.messages_file is not None
        assert "~" not in str(cfg.messages_file)
        assert cfg.messages_file == Path.home() / "msgs.txt"

    def test_toml_credentials_tilde_expanded(self, monkeypatch, credentials_file, tmp_path):
        """Credentials path from TOML should also be tilde-expanded."""
        toml_file = _write_toml(
            tmp_path / "config.toml",
            ('jid = "bot@example.com"\nrecipient = "rcpt@example.com"\ncredentials = "~/fake-cred"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        resolved = _resolve_credentials(None, None, "~/fake-cred")
        assert "~" not in str(resolved)
        assert resolved == Path.home() / "fake-cred"


# ---------------------------------------------------------------------------
# 4. Legacy fallback to ~/.config/xmpp-notify/credentials
# ---------------------------------------------------------------------------


class TestLegacyFallback:
    def test_falls_back_to_legacy_credentials(self, monkeypatch, tmp_path):
        # New path does NOT exist
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "new-config")

        # Legacy path DOES exist
        legacy_dir = tmp_path / "xmpp-notify"
        legacy_dir.mkdir()
        legacy_cred = legacy_dir / "credentials"
        legacy_cred.write_text("legacy-pw")
        legacy_cred.chmod(0o600)
        monkeypatch.setattr(config, "LEGACY_CREDENTIALS_FILE", legacy_cred)

        resolved = _resolve_credentials(None, None, None)
        assert resolved == legacy_cred

    def test_new_path_preferred_over_legacy(self, monkeypatch, tmp_path):
        # Both paths exist — new should win
        new_dir = tmp_path / "new-config"
        new_dir.mkdir()
        new_cred = new_dir / "credentials"
        new_cred.write_text("new-pw")
        new_cred.chmod(0o600)
        monkeypatch.setattr(config, "CONFIG_DIR", new_dir)

        legacy_dir = tmp_path / "xmpp-notify"
        legacy_dir.mkdir()
        legacy_cred = legacy_dir / "credentials"
        legacy_cred.write_text("legacy-pw")
        legacy_cred.chmod(0o600)
        monkeypatch.setattr(config, "LEGACY_CREDENTIALS_FILE", legacy_cred)

        resolved = _resolve_credentials(None, None, None)
        assert resolved == new_cred

    def test_legacy_password_actually_read(self, monkeypatch, tmp_path):
        """Full load_config should read the legacy password successfully."""
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "no-new")

        legacy_dir = tmp_path / "xmpp-notify"
        legacy_dir.mkdir()
        legacy_cred = legacy_dir / "credentials"
        legacy_cred.write_text("legacy-secret")
        legacy_cred.chmod(0o600)
        monkeypatch.setattr(config, "LEGACY_CREDENTIALS_FILE", legacy_cred)
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")

        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config()
        assert cfg.password == "legacy-secret"


# ---------------------------------------------------------------------------
# 5. Missing JID -> SystemExit with clear error
# ---------------------------------------------------------------------------


class TestMissingJID:
    def test_load_config_exits_without_jid(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        with pytest.raises(SystemExit, match="XMPP JID not configured"):
            load_config(cli_credentials=str(credentials_file))

    def test_load_notify_config_exits_without_jid(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        with pytest.raises(SystemExit, match="XMPP JID not configured"):
            load_notify_config(cli_credentials=str(credentials_file))

    def test_error_message_lists_all_sources(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        with pytest.raises(SystemExit, match="--jid") as exc_info:
            load_config(cli_credentials=str(credentials_file))
        msg = str(exc_info.value)
        assert "CLAUDE_XMPP_JID" in msg
        assert "nope.toml" in msg


# ---------------------------------------------------------------------------
# 6. Missing recipient -> SystemExit with clear error
# ---------------------------------------------------------------------------


class TestMissingRecipient:
    def test_load_config_exits_without_recipient(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")

        with pytest.raises(SystemExit, match="XMPP recipient not configured"):
            load_config(cli_credentials=str(credentials_file))

    def test_load_notify_config_exits_without_recipient(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")

        with pytest.raises(SystemExit, match="XMPP recipient not configured"):
            load_notify_config(cli_credentials=str(credentials_file))

    def test_error_message_lists_all_sources(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")

        with pytest.raises(SystemExit, match="--recipient") as exc_info:
            load_config(cli_credentials=str(credentials_file))
        msg = str(exc_info.value)
        assert "CLAUDE_XMPP_RECIPIENT" in msg


# ---------------------------------------------------------------------------
# 7. Permissions warning for credentials with mode > 600
# ---------------------------------------------------------------------------


class TestPermissionsWarning:
    def test_errors_on_group_readable(self, tmp_path):
        """Group-readable credentials file must raise SystemExit (not just warn)."""
        cred = tmp_path / "credentials"
        cred.write_text("secret")
        cred.chmod(0o640)

        with pytest.raises(SystemExit) as exc_info:
            _check_permissions(cred)
        assert "600" in str(exc_info.value)

    def test_errors_on_world_readable(self, tmp_path):
        """World-readable credentials file must raise SystemExit."""
        cred = tmp_path / "credentials"
        cred.write_text("secret")
        cred.chmod(0o644)

        with pytest.raises(SystemExit) as exc_info:
            _check_permissions(cred)
        assert "600" in str(exc_info.value)

    def test_no_error_on_600(self, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("secret")
        cred.chmod(0o600)
        _check_permissions(cred)  # must not raise

    def test_no_error_on_owner_only_400(self, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("secret")
        cred.chmod(0o400)
        _check_permissions(cred)  # must not raise

    def test_no_crash_on_nonexistent_file(self, tmp_path):
        """_check_permissions swallows OSError for non-existent files."""
        fake = tmp_path / "does-not-exist"
        _check_permissions(fake)  # should not raise

    def test_error_raised_during_full_load(self, monkeypatch, tmp_path):
        """SystemExit is raised when load_config reads insecure credentials."""
        cred = tmp_path / "credentials"
        cred.write_text("insecure-pw")
        cred.chmod(0o644)

        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        with pytest.raises(SystemExit) as exc_info:
            load_config(cli_credentials=str(cred))
        assert "600" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. Empty credentials file -> ValueError
# ---------------------------------------------------------------------------


class TestEmptyCredentials:
    def test_empty_file_raises_valueerror(self, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("")
        cred.chmod(0o600)

        with pytest.raises(ValueError, match="Credentials file is empty"):
            _read_password(cred)

    def test_whitespace_only_raises_valueerror(self, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("   \n\t  \n")
        cred.chmod(0o600)

        with pytest.raises(ValueError, match="Credentials file is empty"):
            _read_password(cred)

    def test_empty_credentials_during_full_load(self, monkeypatch, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("")
        cred.chmod(0o600)

        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        with pytest.raises(ValueError, match="Credentials file is empty"):
            load_config(cli_credentials=str(cred))


# ---------------------------------------------------------------------------
# 9. load_notify_config returns NotifyConfig subset
# ---------------------------------------------------------------------------


class TestNotifyConfig:
    def test_returns_notify_config_type(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        ncfg = load_notify_config(cli_credentials=str(credentials_file))
        assert isinstance(ncfg, NotifyConfig)

    def test_has_jid_password_recipient(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        ncfg = load_notify_config(cli_credentials=str(credentials_file))
        assert ncfg.jid == "bot@example.com"
        assert ncfg.password == "test-password"
        assert ncfg.recipient == "rcpt@example.com"

    def test_no_socket_or_db_attrs(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        ncfg = load_notify_config(cli_credentials=str(credentials_file))
        assert not hasattr(ncfg, "socket_path")
        assert not hasattr(ncfg, "db_path")
        assert not hasattr(ncfg, "messages_file")

    def test_cli_overrides_in_notify_config(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "env@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "env-rcpt@example.com")

        ncfg = load_notify_config(
            cli_jid="cli@example.com",
            cli_recipient="cli-rcpt@example.com",
            cli_credentials=str(credentials_file),
        )
        assert ncfg.jid == "cli@example.com"
        assert ncfg.recipient == "cli-rcpt@example.com"

    def test_notify_config_frozen(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        ncfg = load_notify_config(cli_credentials=str(credentials_file))
        with pytest.raises(AttributeError):
            ncfg.jid = "other@example.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 10. TOML config file loading
# ---------------------------------------------------------------------------


class TestTOMLConfig:
    def test_reads_jid_and_recipient_from_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "toml-bot@example.com"\nrecipient = "toml-user@example.com"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.jid == "toml-bot@example.com"
        assert cfg.recipient == "toml-user@example.com"

    def test_reads_credentials_path_from_toml(self, monkeypatch, tmp_path):
        toml_cred = tmp_path / "toml-cred"
        toml_cred.write_text("toml-password")
        toml_cred.chmod(0o600)

        toml_file = _write_toml(
            tmp_path / "config.toml",
            (f'jid = "bot@example.com"\nrecipient = "rcpt@example.com"\ncredentials = "{toml_cred}"\n'),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config()
        assert cfg.password == "toml-password"

    def test_reads_all_paths_from_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            (
                'jid = "bot@example.com"\n'
                'recipient = "rcpt@example.com"\n'
                'socket_path = "/toml/bridge.sock"\n'
                'db_path = "/toml/bridge.db"\n'
                'messages_file = "/toml/messages.txt"\n'
            ),
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.socket_path == Path("/toml/bridge.sock")
        assert cfg.db_path == Path("/toml/bridge.db")
        assert cfg.messages_file == Path("/toml/messages.txt")

    def test_missing_toml_file_returns_empty_dict(self, tmp_path):
        result = _read_toml(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_toml_str_returns_none_for_missing_key(self):
        assert _toml_str({}, "foo") is None

    def test_toml_str_returns_string(self):
        assert _toml_str({"foo": "bar"}, "foo") == "bar"

    def test_toml_str_coerces_non_string(self):
        assert _toml_str({"port": 5222}, "port") == "5222"

    def test_partial_toml_with_env_supplement(self, monkeypatch, credentials_file, tmp_path):
        """TOML has JID, env has recipient — both should be used."""
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "toml@example.com"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "env-rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.jid == "toml@example.com"
        assert cfg.recipient == "env-rcpt@example.com"


# ---------------------------------------------------------------------------
# 11. Default paths for socket/db when not specified
# ---------------------------------------------------------------------------


class TestDefaultPaths:
    def test_default_socket_path(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.socket_path == Path.home() / ".claude" / "bridge.sock"

    def test_default_db_path(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.db_path == Path.home() / ".claude" / "bridge.db"

    def test_default_messages_is_none(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        assert cfg.messages_file is None

    def test_module_level_defaults_match_home(self):
        assert Path.home() / ".claude" / "bridge.sock" == config.DEFAULT_SOCKET_PATH
        assert Path.home() / ".claude" / "bridge.db" == config.DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Extra: Config is frozen
# ---------------------------------------------------------------------------


class TestConfigFrozen:
    def test_config_is_immutable(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))
        with pytest.raises(AttributeError):
            cfg.jid = "other@example.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Extra: Password stripping
# ---------------------------------------------------------------------------


class TestPasswordStripping:
    def test_trailing_newline_stripped(self, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("my-secret\n")
        cred.chmod(0o600)

        assert _read_password(cred) == "my-secret"

    def test_surrounding_whitespace_stripped(self, tmp_path):
        cred = tmp_path / "credentials"
        cred.write_text("  my-secret  \n")
        cred.chmod(0o600)

        assert _read_password(cred) == "my-secret"


# ---------------------------------------------------------------------------
# 13. validate_config pre-flight checks
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def _make_config(self, tmp_path, **overrides):
        defaults = {
            "jid": "bot@example.com",
            "password": "secret",
            "recipient": "user@example.com",
            "socket_path": tmp_path / "bridge.sock",
            "db_path": tmp_path / "bridge.db",
            "messages_file": None,
        }
        defaults.update(overrides)
        return Config(**defaults)

    def test_valid_config_passes(self, tmp_path):
        cfg = self._make_config(tmp_path)
        validate_config(cfg)  # should not raise

    def test_jid_without_at_raises(self, tmp_path):
        cfg = self._make_config(tmp_path, jid="invalid-jid")
        with pytest.raises(SystemExit, match="missing @"):
            validate_config(cfg)

    def test_recipient_without_at_raises(self, tmp_path):
        cfg = self._make_config(tmp_path, recipient="invalid-recipient")
        with pytest.raises(SystemExit, match="missing @"):
            validate_config(cfg)

    def test_both_jid_and_recipient_invalid(self, tmp_path):
        cfg = self._make_config(tmp_path, jid="bad", recipient="bad")
        with pytest.raises(SystemExit, match="Configuration errors") as exc_info:
            validate_config(cfg)
        msg = str(exc_info.value)
        assert "JID" in msg
        assert "Recipient" in msg

    def test_nonexistent_socket_parent_raises(self, tmp_path):
        cfg = self._make_config(tmp_path, socket_path=tmp_path / "nonexistent" / "bridge.sock")
        with pytest.raises(SystemExit, match="Socket path parent"):
            validate_config(cfg)

    def test_nonexistent_db_parent_raises(self, tmp_path):
        cfg = self._make_config(tmp_path, db_path=tmp_path / "nonexistent" / "bridge.db")
        with pytest.raises(SystemExit, match="Database path parent"):
            validate_config(cfg)

    def test_nonexistent_messages_file_raises(self, tmp_path):
        cfg = self._make_config(tmp_path, messages_file=tmp_path / "nonexistent.toml")
        with pytest.raises(SystemExit, match="Messages file does not exist"):
            validate_config(cfg)

    def test_existing_messages_file_passes(self, tmp_path):
        msgs = tmp_path / "messages.toml"
        msgs.write_text("")
        cfg = self._make_config(tmp_path, messages_file=msgs)
        validate_config(cfg)  # should not raise

    def test_warns_without_screen_or_tmux(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        cfg = self._make_config(tmp_path)

        with caplog.at_level("WARNING", logger="claude_xmpp_bridge.config"):
            validate_config(cfg)

        assert "screen" in caplog.text
        assert "tmux" in caplog.text

    def test_no_warning_with_screen(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/screen" if cmd == "screen" else None)
        cfg = self._make_config(tmp_path)

        with caplog.at_level("WARNING", logger="claude_xmpp_bridge.config"):
            validate_config(cfg)

        assert "screen" not in caplog.text

    def test_non_writable_socket_parent_raises(self, tmp_path):
        """Socket parent directory exists but is not writable → error."""
        import os

        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)  # read+execute, no write
        try:
            if os.access(locked_dir, os.W_OK):
                pytest.skip("running as root — permission check skipped")
            cfg = self._make_config(tmp_path, socket_path=locked_dir / "bridge.sock")
            with pytest.raises(SystemExit, match="not writable"):
                validate_config(cfg)
        finally:
            locked_dir.chmod(0o755)

    def test_non_writable_db_parent_raises(self, tmp_path):
        """DB parent directory exists but is not writable → error."""
        import os

        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)
        try:
            if os.access(locked_dir, os.W_OK):
                pytest.skip("running as root — permission check skipped")
            cfg = self._make_config(tmp_path, db_path=locked_dir / "bridge.db")
            with pytest.raises(SystemExit, match="not writable"):
                validate_config(cfg)
        finally:
            locked_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# 14. SMTP email relay configuration
# ---------------------------------------------------------------------------


class TestSMTPConfig:
    """SMTP relay fields load correctly with layered precedence."""

    def test_smtp_defaults_when_not_configured(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.smtp_host == ""  # disabled by default
        assert cfg.smtp_port == 25
        assert cfg.email_threshold == 4000

    def test_smtp_host_from_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "bot@example.com"\nrecipient = "rcpt@example.com"\nsmtp_host = "192.168.33.200"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.smtp_host == "192.168.33.200"

    def test_smtp_port_from_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "bot@example.com"\nrecipient = "rcpt@example.com"\nsmtp_port = 587\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.smtp_port == 587

    def test_email_threshold_from_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "bot@example.com"\nrecipient = "rcpt@example.com"\nemail_threshold = 1000\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.email_threshold == 1000

    def test_smtp_host_from_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_SMTP_HOST", "mail.example.com")

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.smtp_host == "mail.example.com"

    def test_smtp_port_from_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_SMTP_PORT", "2525")

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.smtp_port == 2525

    def test_email_threshold_from_env(self, monkeypatch, credentials_file, tmp_path):
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.toml")
        monkeypatch.setenv("CLAUDE_XMPP_JID", "bot@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_RECIPIENT", "rcpt@example.com")
        monkeypatch.setenv("CLAUDE_XMPP_EMAIL_THRESHOLD", "250")

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.email_threshold == 250

    def test_env_smtp_overrides_toml(self, monkeypatch, credentials_file, tmp_path):
        toml_file = _write_toml(
            tmp_path / "config.toml",
            'jid = "bot@example.com"\nrecipient = "rcpt@example.com"\nsmtp_host = "toml-host"\n',
        )
        monkeypatch.setattr(config, "CONFIG_FILE", toml_file)
        monkeypatch.setenv("CLAUDE_XMPP_SMTP_HOST", "env-host")

        cfg = load_config(cli_credentials=str(credentials_file))

        assert cfg.smtp_host == "env-host"
