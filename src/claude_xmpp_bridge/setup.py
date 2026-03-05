"""Interactive setup wizard for claude-xmpp-bridge."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import secrets
import shutil
import stat
import sys
import sysconfig
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .config import CONFIG_DIR, CONFIG_FILE

HOOKS_DIR = Path.home() / ".claude" / "hooks"
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
SWITCHES_DIR = Path.home() / ".config" / "xmpp-notify"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
OPENCODE_CONFIG_DIR = Path.home() / ".config" / "opencode"
OPENCODE_PLUGINS_DIR = OPENCODE_CONFIG_DIR / "plugins"
OPENCODE_SETTINGS = OPENCODE_CONFIG_DIR / "opencode.json"

# Hook files to install (filename -> target name in ~/.claude/hooks/)
HOOK_FILES: dict[str, str] = {
    "session-start-title.sh": "session-start-title.sh",
    "session-start-register.sh": "session-start-register.sh",
    "session-end.sh": "session-end.sh",
    "notification.sh": "notification.sh",
    "task-completed.sh": "task-completed.sh",
    "stop.sh": "stop.sh",
    "permission-ask.sh": "permission-ask-xmpp.sh",
    "format-location.sh": "format-location.sh",
}


def _find_opencode_dir() -> Path | None:
    """Find the directory containing OpenCode plugin sources."""
    source_dir = Path(__file__).resolve().parent.parent.parent / "examples" / "opencode"
    if source_dir.is_dir() and (source_dir / "plugins" / "xmpp-bridge.js").is_file():
        return source_dir

    data_path = sysconfig.get_path("data")
    if data_path:
        shared_dir = Path(data_path) / "share" / "claude-xmpp-bridge" / "opencode"
        if shared_dir.is_dir():
            return shared_dir

    return None


def _find_hooks_dir() -> Path | None:
    """Find the directory containing hook script sources."""
    # Source tree (editable install)
    source_dir = Path(__file__).resolve().parent.parent.parent / "examples" / "hooks"
    if source_dir.is_dir() and (source_dir / "session-start-title.sh").is_file():
        return source_dir

    # Shared data (pip install)
    data_path = sysconfig.get_path("data")
    if data_path:
        shared_dir = Path(data_path) / "share" / "claude-xmpp-bridge" / "hooks"
        if shared_dir.is_dir():
            return shared_dir

    return None


def _find_settings_json() -> Path | None:
    """Find the example settings.json."""
    hooks_dir = _find_hooks_dir()
    if hooks_dir:
        settings = hooks_dir / "settings.json"
        if settings.is_file():
            return settings
    return None


def _find_sandbox_script() -> Path | None:
    """Find the sandbox script source."""
    source = Path(__file__).resolve().parent.parent.parent / "examples" / "sandbox" / "sandbox"
    if source.is_file():
        return source

    data_path = sysconfig.get_path("data")
    if data_path:
        shared = Path(data_path) / "share" / "claude-xmpp-bridge" / "sandbox" / "sandbox"
        if shared.is_file():
            return shared

    return None


def _find_sandbox_completion() -> Path | None:
    """Find the sandbox bash-completion source file."""
    source = Path(__file__).resolve().parent.parent.parent / "examples" / "sandbox" / "sandbox.bash-completion"
    if source.is_file():
        return source

    data_path = sysconfig.get_path("data")
    if data_path:
        shared = Path(data_path) / "share" / "claude-xmpp-bridge" / "sandbox" / "sandbox.bash-completion"
        if shared.is_file():
            return shared

    return None


def _find_systemd_unit() -> Path | None:
    """Find the example systemd unit file."""
    source_dir = Path(__file__).resolve().parent.parent.parent / "examples" / "systemd"
    unit = source_dir / "claude-xmpp-bridge.service"
    if unit.is_file():
        return unit

    data_path = sysconfig.get_path("data")
    if data_path:
        shared = Path(data_path) / "share" / "claude-xmpp-bridge" / "systemd" / "claude-xmpp-bridge.service"
        if shared.is_file():
            return shared

    return None


def _confirm(prompt: str, default: bool = True, yes_mode: bool = False) -> bool:
    """Ask yes/no question. Returns default in --yes mode."""
    if yes_mode:
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _needs_update(src: Path, dst: Path) -> bool:
    """Return True if dst does not exist or differs from src."""
    if not dst.is_file():
        return True
    return src.read_bytes() != dst.read_bytes()


def _step_credentials(yes_mode: bool) -> bool:
    """Step 1: Create credentials file."""
    print("\n--- Step 1: Credentials ---")
    cred_path = CONFIG_DIR / "credentials"

    if cred_path.is_file():
        print(f"  Credentials file already exists: {cred_path}")
        if not _confirm("  Overwrite?", default=False, yes_mode=False):
            return True

    if yes_mode:
        print("  Skipping (--yes mode, cannot prompt for password)")
        return True

    password = getpass.getpass("  XMPP bot password: ")
    if not password:
        print("  Error: empty password", file=sys.stderr)
        return False

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(password + "\n")
    cred_path.chmod(0o600)
    print(f"  Saved: {cred_path} (mode 600)")
    return True


def _step_config(yes_mode: bool) -> bool:
    """Step 2: Create config.toml."""
    print("\n--- Step 2: Configuration ---")

    if CONFIG_FILE.is_file():
        print(f"  Config file already exists: {CONFIG_FILE}")
        if not _confirm("  Overwrite?", default=False, yes_mode=False):
            return True

    if yes_mode:
        print("  Skipping (--yes mode, cannot prompt for JID)")
        return True

    jid = input("  Bot XMPP JID (e.g. notify-bot@example.com): ").strip()
    if not jid or "@" not in jid:
        print("  Error: invalid JID (must contain @)", file=sys.stderr)
        return False

    recipient = input("  Your XMPP JID (e.g. you@example.com): ").strip()
    if not recipient or "@" not in recipient:
        print("  Error: invalid recipient (must contain @)", file=sys.stderr)
        return False

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    # Escape TOML special characters in user input to prevent injection
    safe_jid = jid.replace("\\", "\\\\").replace('"', '\\"')
    safe_recipient = recipient.replace("\\", "\\\\").replace('"', '\\"')
    CONFIG_FILE.write_text(f'jid = "{safe_jid}"\nrecipient = "{safe_recipient}"\nsocket_token = "{token}"\n')
    CONFIG_FILE.chmod(0o600)

    token_file = CONFIG_DIR / "socket_token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)

    print(f"  Saved: {CONFIG_FILE} (with generated socket_token)")
    return True


def _step_test() -> bool:
    """Step 3: Test XMPP connectivity."""
    print("\n--- Step 3: Test XMPP connection ---")

    from .config import load_notify_config
    from .notify import send_notification

    try:
        config = load_notify_config()
    except (SystemExit, FileNotFoundError, ValueError) as e:
        print(f"  Error loading config: {e}", file=sys.stderr)
        return False

    print(f"  Connecting as {config.jid} → {config.recipient}...")
    try:
        asyncio.run(send_notification(config, "claude-xmpp-bridge setup test"))
        print("  Test message sent successfully!")
        return True
    except ConnectionError as e:
        print(f"  Connection failed: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        return False


def _step_hooks(yes_mode: bool, upgrade: bool = False) -> bool:
    """Step 4: Install hook scripts and merge settings.json."""
    print("\n--- Step 4: Hook scripts ---")

    hooks_source = _find_hooks_dir()
    if not hooks_source:
        print("  Warning: hook source files not found, skipping")
        return True

    if not upgrade and not _confirm("  Install hook scripts to ~/.claude/hooks/?", yes_mode=yes_mode):
        return True

    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    updated = 0
    up_to_date = 0
    for src_name, dst_name in HOOK_FILES.items():
        src = hooks_source / src_name
        dst = HOOKS_DIR / dst_name
        if not src.is_file():
            continue
        if not _needs_update(src, dst):
            up_to_date += 1
            continue
        if not upgrade and dst.is_file() and not yes_mode and not _confirm(f"    Overwrite {dst.name}?", default=False):
            continue
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        updated += 1
        if upgrade:
            print(f"    {dst.name}: updated")

    if upgrade:
        if up_to_date and not updated:
            print(f"  All {up_to_date} hook scripts up to date")
        elif updated:
            print(f"  {updated} updated, {up_to_date} up to date")
    else:
        print(f"  Installed {updated} hook scripts to {HOOKS_DIR}")

    # Merge settings.json
    settings_src = _find_settings_json()
    if not settings_src:
        return True

    if not _confirm("  Merge hook config into ~/.claude/settings.json?", yes_mode=yes_mode):
        return True

    try:
        new_hooks = json.loads(settings_src.read_text())["hooks"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  Error reading hook config: {e}", file=sys.stderr)
        return True

    existing: dict[str, object] = {}
    if CLAUDE_SETTINGS.is_file():
        try:
            existing = json.loads(CLAUDE_SETTINGS.read_text())
        except json.JSONDecodeError:
            print(f"  Warning: could not parse {CLAUDE_SETTINGS}, creating new")

    if "hooks" not in existing:
        existing["hooks"] = {}

    existing_hooks = existing["hooks"]
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}
        existing["hooks"] = existing_hooks

    for event, hook_list in new_hooks.items():
        existing_hooks[event] = hook_list

    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"  Updated: {CLAUDE_SETTINGS}")
    return True


def _step_opencode(yes_mode: bool, upgrade: bool = False) -> bool:
    """Step 5: Install OpenCode plugin and config."""
    print("\n--- Step 5: OpenCode plugin ---")

    opencode_source = _find_opencode_dir()
    if not opencode_source:
        print("  Warning: OpenCode plugin source files not found, skipping")
        return True

    if not upgrade and not _confirm(
        "  Install OpenCode XMPP plugin to ~/.config/opencode/plugins/?", yes_mode=yes_mode
    ):
        return True

    OPENCODE_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    src = opencode_source / "plugins" / "xmpp-bridge.js"
    dst = OPENCODE_PLUGINS_DIR / "xmpp-bridge.js"
    if not _needs_update(src, dst):
        if upgrade:
            print("  xmpp-bridge.js: up to date")
    elif upgrade or yes_mode or not dst.is_file() or _confirm("    Overwrite xmpp-bridge.js?", default=False):
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
        print(f"  xmpp-bridge.js: {'updated' if upgrade else 'installed'}")

    # Merge permission config into opencode.json
    config_src = opencode_source / "opencode.json"
    if not config_src.is_file():
        return True

    if not _confirm("  Merge permission config into ~/.config/opencode/opencode.json?", yes_mode=yes_mode):
        return True

    try:
        new_permission = json.loads(config_src.read_text())["permission"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  Error reading opencode.json: {e}", file=sys.stderr)
        return True

    existing: dict[str, object] = {}
    if OPENCODE_SETTINGS.is_file():
        try:
            existing = json.loads(OPENCODE_SETTINGS.read_text())
        except json.JSONDecodeError:
            print(f"  Warning: could not parse {OPENCODE_SETTINGS}, creating new")

    existing["permission"] = new_permission

    OPENCODE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    OPENCODE_SETTINGS.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"  Updated: {OPENCODE_SETTINGS}")
    return True


def _step_systemd(yes_mode: bool, upgrade: bool = False) -> bool:
    """Step 6: Install systemd user unit."""
    print("\n--- Step 6: systemd service ---")

    if not shutil.which("systemctl"):
        print("  systemctl not found, skipping")
        return True

    unit_src = _find_systemd_unit()
    if not unit_src:
        print("  Warning: systemd unit file not found, skipping")
        return True

    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    dst = SYSTEMD_DIR / "claude-xmpp-bridge.service"

    if not _needs_update(unit_src, dst):
        if upgrade:
            print("  systemd unit: up to date")
        return True

    if not upgrade and not _confirm("  Install systemd user service?", yes_mode=yes_mode):
        return True

    if not upgrade and dst.is_file() and not yes_mode and not _confirm(f"    Overwrite {dst}?", default=False):
        return True

    shutil.copy2(unit_src, dst)
    print(f"  systemd unit: {'updated' if upgrade else 'installed'}")
    print("  Run: systemctl --user daemon-reload")
    return True


def _step_sandbox(yes_mode: bool, upgrade: bool = False) -> bool:
    """Step 7: Install sandbox script and bash completion."""
    print("\n--- Step 7: Sandbox script ---")

    sandbox_src = _find_sandbox_script()
    if not sandbox_src:
        print("  Warning: sandbox script source not found, skipping")
        return True

    dst = Path.home() / ".local" / "bin" / "sandbox"

    if _needs_update(sandbox_src, dst):
        if upgrade or yes_mode or not dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sandbox_src, dst)
            dst.chmod(0o755)
            print(f"  sandbox: {'updated' if upgrade else 'installed'}")
        elif _confirm("  Overwrite sandbox script?", default=False):
            shutil.copy2(sandbox_src, dst)
            dst.chmod(0o755)
            print("  sandbox: updated")
        else:
            return True
    elif upgrade:
        print("  sandbox: up to date")

    # Install bash completion
    comp_src = _find_sandbox_completion()
    if comp_src:
        comp_dir = Path.home() / ".local" / "share" / "bash-completion" / "completions"
        comp_dst = comp_dir / "sandbox"
        if _needs_update(comp_src, comp_dst):
            comp_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(comp_src, comp_dst)
            print(f"  bash completion: {'updated' if upgrade else 'installed'}")
        elif upgrade:
            print("  bash completion: up to date")

    return True


def _step_switches(yes_mode: bool) -> bool:
    """Step 8: Enable on/off switches."""
    print("\n--- Step 8: Notification switches ---")

    SWITCHES_DIR.mkdir(parents=True, exist_ok=True)

    switches = {
        "notify-enabled": "Notifications, task completion, stop messages",
        "ask-enabled": "Permission requests via XMPP",
    }

    for name, desc in switches.items():
        path = SWITCHES_DIR / name
        if path.is_file():
            print(f"  {name}: already enabled ({desc})")
        elif _confirm(f"  Enable {name}? ({desc})", yes_mode=yes_mode):
            path.touch()
            print(f"  Enabled: {name}")
        else:
            print(f"  Skipped: {name}")

    return True


def setup_main() -> None:
    """Entry point for claude-xmpp-bridge-setup."""
    parser = argparse.ArgumentParser(
        description="Interactive setup wizard for claude-xmpp-bridge",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--yes", "-y", action="store_true", help="Accept defaults without prompting")
    parser.add_argument(
        "--upgrade", "-u", action="store_true", help="Upgrade managed files (overwrite changed, skip identical)"
    )
    parser.add_argument("--test-only", action="store_true", help="Only test XMPP connectivity")
    args = parser.parse_args()

    if args.test_only:
        ok = _step_test()
        sys.exit(0 if ok else 1)

    upgrade = args.upgrade

    print(f"claude-xmpp-bridge setup wizard v{__version__}")
    print("=" * 40)

    if upgrade:
        print("\nUpgrade mode: updating managed files...")
        steps: list[tuple[str, Callable[[], bool]]] = [
            ("hooks", lambda: _step_hooks(yes_mode=True, upgrade=True)),
            ("opencode", lambda: _step_opencode(yes_mode=True, upgrade=True)),
            ("systemd", lambda: _step_systemd(yes_mode=True, upgrade=True)),
            ("sandbox", lambda: _step_sandbox(yes_mode=True, upgrade=True)),
        ]
    else:
        mode = "1"
        if not args.yes:
            print("\nWhat do you want to install?")
            print("  1) Full XMPP bridge (routes messages to your Jabber client)")
            print("  2) Local tools only (nice screen titles, OpenCode plugin, sandbox script)")
            choice = input("Choose [1/2] (default 1): ").strip()
            if choice in ("1", "2"):
                mode = choice

        if mode == "1":
            steps = [
                ("credentials", lambda: _step_credentials(args.yes)),
                ("config", lambda: _step_config(args.yes)),
                ("test", _step_test),
                ("hooks", lambda: _step_hooks(args.yes)),
                ("opencode", lambda: _step_opencode(args.yes)),
                ("systemd", lambda: _step_systemd(args.yes)),
                ("sandbox", lambda: _step_sandbox(args.yes)),
                ("switches", lambda: _step_switches(args.yes)),
            ]
        else:
            print("\n--- Installing Local Tools Only ---")
            steps = [
                ("hooks", lambda: _step_hooks(args.yes)),
                ("opencode", lambda: _step_opencode(args.yes)),
                ("sandbox", lambda: _step_sandbox(args.yes)),
            ]

    for name, step_fn in steps:
        try:
            ok = step_fn()
        except KeyboardInterrupt:
            print("\n\nSetup interrupted.")
            sys.exit(1)
        except Exception as e:
            print(f"\n  Error in {name}: {e}", file=sys.stderr)
            ok = False

        if not ok and not _confirm(f"\n  Step '{name}' failed. Continue anyway?", yes_mode=args.yes or upgrade):
            sys.exit(1)

    print("\n" + "=" * 40)
    if upgrade:
        print("Upgrade complete!")
    else:
        print("Setup complete!")
        print("\nNext steps:")
        print("  1. Start the bridge: claude-xmpp-bridge")
        print("  2. Or via systemd:   systemctl --user start claude-xmpp-bridge")
        print("  3. Test:             claude-xmpp-notify 'Hello from bridge!'")
