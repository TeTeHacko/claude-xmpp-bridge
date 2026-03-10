"""Layered configuration: CLI args > env vars > TOML config > defaults."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "claude-xmpp-bridge"
CONFIG_FILE = CONFIG_DIR / "config.toml"
LEGACY_CREDENTIALS_FILE = Path.home() / ".config" / "xmpp-notify" / "credentials"
DEFAULT_SOCKET_PATH = Path.home() / ".claude" / "bridge.sock"
DEFAULT_DB_PATH = Path.home() / ".claude" / "bridge.db"
DEFAULT_MCP_PORT = 7878
DEFAULT_SMTP_PORT = 25
EMAIL_THRESHOLD_DEFAULT = 500


# Default icons per source value. None key = fallback for unknown/unset source.
DEFAULT_SOURCE_ICONS: dict[str | None, str] = {
    "opencode": "🧠",
    None: "⚡",
}

# Maximum allowed length for a source field value
MAX_SOURCE_LEN = 64


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
    audit_log: str = "journald"  # "journald" or path to a rotating JSON Lines file
    mcp_port: int = DEFAULT_MCP_PORT  # port for MCP HTTP server (0 = disabled)
    # Email relay: when a notification exceeds email_threshold chars, the bridge
    # sends the full text via SMTP and truncates the XMPP message to a snippet.
    # Set smtp_host = "" (default) to disable email relay entirely.
    smtp_host: str = ""  # SMTP relay host; empty string = disabled
    smtp_port: int = DEFAULT_SMTP_PORT
    email_threshold: int = EMAIL_THRESHOLD_DEFAULT  # chars; 0 = always email
    # Per-source icons: keys are source strings (or None for default/unknown).
    # Loaded from [source_icons] TOML section; missing keys fall back to DEFAULT_SOURCE_ICONS.
    source_icons: dict[str | None, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        token_repr = "'***'" if self.socket_token else "None"
        return (
            f"Config(jid={self.jid!r}, password='***', recipient={self.recipient!r}, "
            f"socket_path={self.socket_path!r}, db_path={self.db_path!r}, "
            f"messages_file={self.messages_file!r}, socket_token={token_repr}, "
            f"force_starttls={self.force_starttls!r}, audit_log={self.audit_log!r}, "
            f"mcp_port={self.mcp_port!r}, "
            f"smtp_host={self.smtp_host!r}, smtp_port={self.smtp_port!r}, "
            f"email_threshold={self.email_threshold!r}, "
            f"source_icons={self.source_icons!r})"
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
    except OSError as exc:
        log.warning("Cannot check permissions for %s: %s", path, exc)


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
    cli_mcp_port: int | None = None,
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
    if force_starttls_raw is None:
        force_starttls = True
    elif isinstance(force_starttls_raw, bool):
        force_starttls = force_starttls_raw
    else:
        raise SystemExit(
            f"Error: force_starttls must be a boolean (true/false) in {CONFIG_FILE}, "
            f"got {type(force_starttls_raw).__name__}: {force_starttls_raw!r}"
        )

    # Audit log destination: "journald" (default) or path to a file
    audit_log = os.environ.get("CLAUDE_XMPP_AUDIT_LOG") or _toml_str(toml, "audit_log") or "journald"

    # MCP server port (0 = disabled)
    mcp_port_env = os.environ.get("CLAUDE_XMPP_MCP_PORT")
    mcp_port_toml = toml.get("mcp_port")
    if cli_mcp_port is not None:
        mcp_port = cli_mcp_port
    elif mcp_port_env:
        try:
            mcp_port = int(mcp_port_env)
        except ValueError:
            raise SystemExit(f"Error: CLAUDE_XMPP_MCP_PORT must be an integer, got {mcp_port_env!r}") from None
    elif mcp_port_toml is not None:
        mcp_port = int(str(mcp_port_toml))
    else:
        mcp_port = DEFAULT_MCP_PORT

    # SMTP email relay (optional — empty smtp_host disables email)
    smtp_host = os.environ.get("CLAUDE_XMPP_SMTP_HOST") or _toml_str(toml, "smtp_host") or ""
    smtp_port_env = os.environ.get("CLAUDE_XMPP_SMTP_PORT")
    smtp_port_toml = toml.get("smtp_port")
    if smtp_port_env:
        try:
            smtp_port = int(smtp_port_env)
        except ValueError:
            raise SystemExit(f"Error: CLAUDE_XMPP_SMTP_PORT must be an integer, got {smtp_port_env!r}") from None
    elif smtp_port_toml is not None:
        smtp_port = int(str(smtp_port_toml))
    else:
        smtp_port = DEFAULT_SMTP_PORT
    email_threshold_env = os.environ.get("CLAUDE_XMPP_EMAIL_THRESHOLD")
    email_threshold_toml = toml.get("email_threshold")
    if email_threshold_env:
        try:
            email_threshold = int(email_threshold_env)
        except ValueError:
            raise SystemExit(
                f"Error: CLAUDE_XMPP_EMAIL_THRESHOLD must be an integer, got {email_threshold_env!r}"
            ) from None
    elif email_threshold_toml is not None:
        email_threshold = int(str(email_threshold_toml))
    else:
        email_threshold = EMAIL_THRESHOLD_DEFAULT

    # Source icons: loaded from [source_icons] TOML section.
    # Keys are source strings; special key "default" maps to None (unknown/unset source).
    # Example:
    #   [source_icons]
    #   opencode = "🧠"
    #   cursor   = "🔵"
    #   default  = "⚡"
    source_icons: dict[str | None, str] = {}
    raw_icons = toml.get("source_icons")
    if isinstance(raw_icons, dict):
        for k, v in raw_icons.items():
            if isinstance(k, str) and isinstance(v, str):
                actual_key: str | None = None if k == "default" else k
                source_icons[actual_key] = v

    return Config(
        jid=jid,
        password=password,
        recipient=recipient,
        socket_path=socket_path,
        db_path=db_path,
        messages_file=messages_file,
        socket_token=socket_token,
        force_starttls=force_starttls,
        audit_log=audit_log,
        mcp_port=mcp_port,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        email_threshold=email_threshold,
        source_icons=source_icons,
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

    if cfg.audit_log != "journald":
        audit_parent = Path(cfg.audit_log).expanduser().parent
        if not audit_parent.is_dir():
            errors.append(f"Audit log parent directory does not exist: {audit_parent}")
        elif not os.access(audit_parent, os.W_OK):
            errors.append(f"Audit log parent directory is not writable: {audit_parent}")

    if errors:
        raise SystemExit("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    if not shutil.which("screen") and not shutil.which("tmux"):
        log.warning("Neither 'screen' nor 'tmux' found in PATH — message delivery will fail")


def _toml_str(toml: dict[str, object], key: str) -> str | None:
    """Get a string value from TOML dict, or None.

    Non-string values are coerced via ``str()`` for backwards compatibility
    (e.g. ``port = 5222`` becomes ``"5222"``).  Returns ``None`` when the
    key is absent.
    """
    val = toml.get(key)
    if val is None:
        return None
    if isinstance(val, str):
        return val
    log.warning("Config key %r has non-string type %s, coercing to string", key, type(val).__name__)
    return str(val)
