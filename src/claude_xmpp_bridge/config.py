"""Layered configuration: CLI args > env vars > TOML config > defaults."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "claude-xmpp-bridge"
CONFIG_FILE = CONFIG_DIR / "config.toml"
LEGACY_CREDENTIALS_FILE = Path.home() / ".config" / "xmpp-notify" / "credentials"
DEFAULT_SOCKET_PATH = Path.home() / ".claude" / "bridge.sock"
DEFAULT_DB_PATH = Path.home() / ".claude" / "bridge.db"


@dataclass(frozen=True)
class Config:
    """Full bridge configuration."""

    jid: str
    password: str
    recipient: str
    socket_path: Path
    db_path: Path
    messages_file: Path | None
    socket_token: str | None = None  # shared secret for socket auth (None = disabled)
    force_starttls: bool = True  # require TLS on XMPP connection

    def __repr__(self) -> str:
        token_repr = "'***'" if self.socket_token else "None"
        return (
            f"Config(jid={self.jid!r}, password='***', recipient={self.recipient!r}, "
            f"socket_path={self.socket_path!r}, db_path={self.db_path!r}, "
            f"messages_file={self.messages_file!r}, socket_token={token_repr}, "
            f"force_starttls={self.force_starttls!r})"
        )


@dataclass(frozen=True)
class NotifyConfig:
    """Subset of config for notify/ask commands."""

    jid: str
    password: str
    recipient: str

    def __repr__(self) -> str:
        return f"NotifyConfig(jid={self.jid!r}, password='***', recipient={self.recipient!r})"


def _read_toml(path: Path) -> dict[str, object]:
    """Read a TOML file, returning empty dict if it doesn't exist."""
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _check_permissions(path: Path) -> None:
    """Error if credentials file is readable by group or others (should be 600)."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SystemExit(
                f"Error: credentials file {path} has permissions {stat.S_IMODE(mode):o}, "
                "which allows group/other access.\n"
                f"Fix with: chmod 600 {path}"
            )
    except OSError:
        pass


def _resolve_credentials(
    cli_credentials: str | None,
    env_credentials: str | None,
    toml_credentials: str | None,
) -> Path:
    """Find the credentials file path from config layers."""
    for source in (cli_credentials, env_credentials, toml_credentials):
        if source:
            return Path(source).expanduser()

    # Default locations
    new_path = CONFIG_DIR / "credentials"
    if new_path.is_file():
        return new_path

    # Legacy fallback
    if LEGACY_CREDENTIALS_FILE.is_file():
        return LEGACY_CREDENTIALS_FILE

    raise FileNotFoundError(
        "Credentials file not found. Create one at:\n"
        f"  {new_path}\n"
        "or (legacy):\n"
        f"  {LEGACY_CREDENTIALS_FILE}\n"
        "with your XMPP password, then: chmod 600 <file>"
    )


def _read_password(credentials_path: Path) -> str:
    """Read password from credentials file."""
    _check_permissions(credentials_path)
    text = credentials_path.read_text().strip()
    if not text:
        raise ValueError(f"Credentials file is empty: {credentials_path}")
    return text


def load_config(
    *,
    cli_jid: str | None = None,
    cli_recipient: str | None = None,
    cli_credentials: str | None = None,
    cli_socket_path: str | None = None,
    cli_db_path: str | None = None,
    cli_messages: str | None = None,
) -> Config:
    """Load full bridge config with layered precedence: CLI > env > TOML > defaults."""
    toml = _read_toml(CONFIG_FILE)

    # JID
    jid = cli_jid or os.environ.get("CLAUDE_XMPP_JID") or _toml_str(toml, "jid")
    if not jid:
        raise SystemExit(
            "Error: XMPP JID not configured.\n"
            "Set it via:\n"
            "  --jid flag\n"
            "  CLAUDE_XMPP_JID environment variable\n"
            f'  jid = "..." in {CONFIG_FILE}'
        )

    # Recipient
    recipient = cli_recipient or os.environ.get("CLAUDE_XMPP_RECIPIENT") or _toml_str(toml, "recipient")
    if not recipient:
        raise SystemExit(
            "Error: XMPP recipient not configured.\n"
            "Set it via:\n"
            "  --recipient flag\n"
            "  CLAUDE_XMPP_RECIPIENT environment variable\n"
            f'  recipient = "..." in {CONFIG_FILE}'
        )

    # Password
    credentials_path = _resolve_credentials(
        cli_credentials,
        os.environ.get("CLAUDE_XMPP_CREDENTIALS"),
        _toml_str(toml, "credentials"),
    )
    password = _read_password(credentials_path)

    # Paths
    socket_path = Path(
        cli_socket_path
        or os.environ.get("CLAUDE_XMPP_SOCKET")
        or _toml_str(toml, "socket_path")
        or str(DEFAULT_SOCKET_PATH)
    ).expanduser()

    db_path = Path(
        cli_db_path or os.environ.get("CLAUDE_XMPP_DB") or _toml_str(toml, "db_path") or str(DEFAULT_DB_PATH)
    ).expanduser()

    # Messages file
    messages_raw = cli_messages or os.environ.get("CLAUDE_XMPP_MESSAGES") or _toml_str(toml, "messages_file")
    messages_file = Path(messages_raw).expanduser() if messages_raw else None

    # Socket token (shared secret for socket authentication)
    socket_token = os.environ.get("CLAUDE_XMPP_SOCKET_TOKEN") or _toml_str(toml, "socket_token") or None

    # Force STARTTLS (default True)
    force_starttls_raw = toml.get("force_starttls")
    force_starttls = bool(force_starttls_raw) if force_starttls_raw is not None else True

    return Config(
        jid=jid,
        password=password,
        recipient=recipient,
        socket_path=socket_path,
        db_path=db_path,
        messages_file=messages_file,
        socket_token=socket_token,
        force_starttls=force_starttls,
    )


def load_notify_config(
    *,
    cli_jid: str | None = None,
    cli_recipient: str | None = None,
    cli_credentials: str | None = None,
) -> NotifyConfig:
    """Load notify/ask config (subset without socket/db paths)."""
    toml = _read_toml(CONFIG_FILE)

    jid = cli_jid or os.environ.get("CLAUDE_XMPP_JID") or _toml_str(toml, "jid")
    if not jid:
        raise SystemExit("Error: XMPP JID not configured.\nSet it via --jid, CLAUDE_XMPP_JID, or in config.toml")

    recipient = cli_recipient or os.environ.get("CLAUDE_XMPP_RECIPIENT") or _toml_str(toml, "recipient")
    if not recipient:
        raise SystemExit(
            "Error: XMPP recipient not configured.\nSet it via --recipient, CLAUDE_XMPP_RECIPIENT, or in config.toml"
        )

    credentials_path = _resolve_credentials(
        cli_credentials,
        os.environ.get("CLAUDE_XMPP_CREDENTIALS"),
        _toml_str(toml, "credentials"),
    )
    password = _read_password(credentials_path)

    return NotifyConfig(jid=jid, password=password, recipient=recipient)


def validate_config(cfg: Config) -> None:
    """Pre-flight validation of config values. Exits on errors, logs warnings."""
    errors: list[str] = []

    if "@" not in cfg.jid:
        errors.append(f"JID {cfg.jid!r} does not look like a valid XMPP address (missing @)")
    if "@" not in cfg.recipient:
        errors.append(f"Recipient {cfg.recipient!r} does not look like a valid XMPP address (missing @)")

    socket_parent = cfg.socket_path.parent
    if not socket_parent.is_dir():
        errors.append(f"Socket path parent directory does not exist: {socket_parent}")
    elif not os.access(socket_parent, os.W_OK):
        errors.append(f"Socket path parent directory is not writable: {socket_parent}")

    db_parent = cfg.db_path.parent
    if not db_parent.is_dir():
        errors.append(f"Database path parent directory does not exist: {db_parent}")
    elif not os.access(db_parent, os.W_OK):
        errors.append(f"Database path parent directory is not writable: {db_parent}")

    if cfg.messages_file and not cfg.messages_file.is_file():
        errors.append(f"Messages file does not exist: {cfg.messages_file}")

    if errors:
        raise SystemExit("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    if not shutil.which("screen") and not shutil.which("tmux"):
        log.warning("Neither 'screen' nor 'tmux' found in PATH — message delivery will fail")


def _toml_str(toml: dict[str, object], key: str) -> str | None:
    """Get a string value from TOML dict, or None."""
    val = toml.get(key)
    return str(val) if val is not None else None
