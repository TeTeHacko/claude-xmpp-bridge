"""Interactive setup wizard for claude-xmpp-bridge."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import secrets
import shutil
import stat
import subprocess
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
LEGACY_OPENCODE_DATA_DIR = Path.home() / ".local" / "share" / "claude-xmpp-bridge" / "opencode"
LEGACY_OPENCODE_PLUGIN_DST = LEGACY_OPENCODE_DATA_DIR / "plugins" / "xmpp-bridge.js"
SANDBOX_DST = Path.home() / ".local" / "bin" / "sandbox"
SANDBOX_COMPLETION_DST = Path.home() / ".local" / "share" / "bash-completion" / "completions" / "sandbox"

# Hook files to install (source name → target name in ~/.claude/hooks/)
HOOK_FILES_BRIDGE: dict[str, str] = {
    "session-start-register.sh": "session-start-register.sh",
    "session-end.sh": "session-end.sh",
    "notification.sh": "notification.sh",
    "task-completed.sh": "task-completed.sh",
    "stop.sh": "stop.sh",
    "permission-ask.sh": "permission-ask-xmpp.sh",
    "format-location.sh": "format-location.sh",
}

HOOK_FILES_LOCAL: dict[str, str] = {
    "session-start-title.sh": "session-start-title.sh",
}

# All hook target names (for uninstall)
ALL_HOOK_TARGETS: list[str] = [
    "session-start-title.sh",
    "session-start-register.sh",
    "session-end.sh",
    "notification.sh",
    "task-completed.sh",
    "stop.sh",
    "permission-ask-xmpp.sh",
    "format-location.sh",
]

# Hook event keys managed by this package (for settings.json removal)
MANAGED_HOOK_EVENTS: list[str] = [
    "SessionStart",
    "SessionEnd",
    "Notification",
    "TaskCompleted",
    "Stop",
    "PermissionRequest",
]

# Components
COMPONENT_SANDBOX = "sandbox"
COMPONENT_HOOKS = "claude-hooks"
COMPONENT_OPENCODE = "opencode-plugin"
COMPONENT_BRIDGE = "bridge-daemon"

ALL_COMPONENTS = [COMPONENT_SANDBOX, COMPONENT_HOOKS, COMPONENT_OPENCODE, COMPONENT_BRIDGE]

PLUGIN_MODE_NORMAL = "normal"
PLUGIN_MODE_TITLE_ONLY = "title-only"


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
    source_dir = Path(__file__).resolve().parent.parent.parent / "examples" / "hooks"
    if source_dir.is_dir() and (source_dir / "session-start-title.sh").is_file():
        return source_dir

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
    """Return True if dst does not exist or differs from src.

    For symlinks, checks that the link target matches ``src.resolve()``.
    For plain files, compares contents byte-by-byte.
    """
    if dst.is_symlink():
        try:
            return dst.resolve() != src.resolve()
        except OSError:
            return True  # dangling symlink
    if not dst.is_file():
        return True
    return src.read_bytes() != dst.read_bytes()


def _install_symlink(src: Path, dst: Path, *, make_executable: bool = False) -> bool:
    """Install *src* as a symlink at *dst*.

    If *dst* is already a correct symlink nothing happens and ``False`` is
    returned.  Otherwise any existing file/symlink is replaced and ``True``
    is returned.

    When *make_executable* is ``True`` the source file gets ``+x`` bits
    (symlinks inherit the target's permissions).
    """
    canonical = src.resolve()
    if dst.is_symlink():
        try:
            if dst.resolve() == canonical:
                return False  # already up to date
        except OSError:
            pass  # dangling — will be replaced

    # Remove old file/symlink before creating the new symlink
    if dst.exists() or dst.is_symlink():
        dst.unlink()

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(canonical)

    if make_executable and canonical.is_file():
        mode = canonical.stat().st_mode
        if not (mode & stat.S_IXUSR):
            canonical.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return True


def _ask_components(yes_mode: bool, default_all: bool = True) -> set[str]:
    """Interactively ask which components to select.

    In yes_mode all components are selected. Otherwise shows a numbered
    toggle menu where the user can deselect unwanted components.
    Returns a set of selected component names.
    """
    if yes_mode:
        return set(ALL_COMPONENTS)

    descriptions = {
        COMPONENT_SANDBOX: "bwrap sandbox launcher + bash tab-completion",
        COMPONENT_HOOKS: "Claude Code hook scripts (title; bridge hooks if bridge-daemon selected)",
        COMPONENT_OPENCODE: "OpenCode plugin (title always; XMPP if bridge running)",
        COMPONENT_BRIDGE: "XMPP bridge daemon: credentials, config, systemd service, switches",
    }

    selected: set[str] = set(ALL_COMPONENTS) if default_all else set()

    print("\nSelect components to install (toggle by number, Enter to confirm):")
    while True:
        for i, comp in enumerate(ALL_COMPONENTS, 1):
            mark = "x" if comp in selected else " "
            print(f"  [{mark}] {i}) {comp:20s}  {descriptions[comp]}")
        print("  Enter = confirm, or enter number(s) to toggle (e.g. '3' or '1 4')")
        raw = input("> ").strip()
        if not raw:
            break
        for token in raw.split():
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(ALL_COMPONENTS):
                    comp = ALL_COMPONENTS[idx]
                    if comp in selected:
                        selected.discard(comp)
                    else:
                        selected.add(comp)

    return selected


# ---------------------------------------------------------------------------
# Install steps
# ---------------------------------------------------------------------------


def _step_credentials(yes_mode: bool) -> bool:
    """Step: Create credentials file."""
    print("\n--- bridge-daemon: Credentials ---")
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
    """Step: Create config.toml."""
    print("\n--- bridge-daemon: Configuration ---")

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
    """Step: Test XMPP connectivity."""
    print("\n--- bridge-daemon: Test XMPP connection ---")

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


def _step_hooks(yes_mode: bool, upgrade: bool = False, with_bridge: bool = True) -> bool:
    """Step: Install hook scripts as symlinks and merge settings.json.

    Since v0.8.15 hooks are installed as **symlinks** pointing to the
    canonical source files, so ``pipx upgrade`` automatically propagates
    hook updates.

    When with_bridge is False only session-start-title.sh is installed
    (the other hooks require the bridge daemon to be useful).
    """
    print("\n--- claude-hooks: Hook scripts ---")

    hooks_source = _find_hooks_dir()
    if not hooks_source:
        print("  Warning: hook source files not found, skipping")
        return True

    # Determine which hook files to install
    hook_files: dict[str, str] = dict(HOOK_FILES_LOCAL)
    if with_bridge:
        hook_files.update(HOOK_FILES_BRIDGE)

    if not with_bridge:
        print("  Note: installing title hook only (bridge-daemon not selected)")

    if not upgrade and not _confirm("  Install hook scripts to ~/.claude/hooks/?", yes_mode=yes_mode):
        return True

    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    updated = 0
    up_to_date = 0
    for src_name, dst_name in hook_files.items():
        src = hooks_source / src_name
        dst = HOOKS_DIR / dst_name
        if not src.is_file():
            continue
        if (
            not upgrade
            and dst.exists()
            and not dst.is_symlink()
            and not yes_mode
            and not _confirm(f"    Replace {dst.name} with symlink?", default=True)
        ):
            continue
        changed = _install_symlink(src, dst, make_executable=True)
        if changed:
            updated += 1
            if upgrade:
                print(f"    {dst.name}: updated (symlink → {src.resolve()})")
        else:
            up_to_date += 1

    if upgrade:
        if up_to_date and not updated:
            print(f"  All {up_to_date} hook scripts up to date")
        elif updated:
            print(f"  {updated} updated, {up_to_date} up to date")
    else:
        print(f"  Installed {updated} hook script(s) to {HOOKS_DIR}")

    # Merge settings.json (only for hooks that were actually installed)
    settings_src = _find_settings_json()
    if not settings_src:
        return True

    if not _confirm("  Merge hook config into ~/.claude/settings.json?", yes_mode=yes_mode):
        return True

    try:
        all_hooks = json.loads(settings_src.read_text())["hooks"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  Error reading hook config: {e}", file=sys.stderr)
        return True

    # Include only events that reference at least one installed hook target.
    # We do a simple substring search in the JSON dump — reliable and structure-agnostic.
    installed_targets = set(hook_files.values())
    new_hooks: dict[str, object] = {}
    for event, hook_list in all_hooks.items():
        hook_json = json.dumps(hook_list)
        if any(target in hook_json for target in installed_targets):
            new_hooks[event] = hook_list

    # If filtering yielded nothing (JSON structure differs), fall back to all hooks
    if not new_hooks:
        new_hooks = all_hooks

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


def _resolve_plugin_source(opencode_dir: Path) -> Path:
    """Return the canonical path to the OpenCode plugin JS file."""
    return (opencode_dir / "plugins" / "xmpp-bridge.js").resolve()


def _step_opencode(yes_mode: bool, upgrade: bool = False, plugin_mode: str = PLUGIN_MODE_NORMAL) -> bool:
    """Step: Install OpenCode plugin as a symlink and merge config.

    Since v0.8.14 the plugin is installed as a **symlink** pointing to the
    canonical source file (either the repository checkout for editable installs
    or the shared-data copy inside the pipx/venv).  This means ``pipx upgrade``
    automatically propagates plugin updates — no manual copy needed.

    The ``plugin_mode`` parameter no longer patches the JS source.  When
    ``plugin_mode == PLUGIN_MODE_TITLE_ONLY`` the function prints a reminder
    to set ``XMPP_BRIDGE_MODE=title-only`` in the environment.
    """
    print("\n--- opencode-plugin: OpenCode plugin ---")

    opencode_source = _find_opencode_dir()
    if not opencode_source:
        print("  Warning: OpenCode plugin source files not found, skipping")
        return True

    if not upgrade and not _confirm("  Install OpenCode plugin to ~/.config/opencode/plugins/?", yes_mode=yes_mode):
        return True

    OPENCODE_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    canonical_src = _resolve_plugin_source(opencode_source)
    dst = OPENCODE_PLUGINS_DIR / "xmpp-bridge.js"

    if (
        not upgrade
        and dst.exists()
        and not dst.is_symlink()
        and not yes_mode
        and not _confirm("    Replace xmpp-bridge.js with symlink?", default=True)
    ):
        pass  # user declined
    else:
        changed = _install_symlink(Path(canonical_src), dst)
        if changed:
            action = "updated" if upgrade else "installed"
            print(f"  xmpp-bridge.js: {action} (symlink → {canonical_src})")
        elif upgrade:
            print(f"  xmpp-bridge.js: up to date (symlink → {canonical_src})")

    if plugin_mode == PLUGIN_MODE_TITLE_ONLY:
        print("  Bridge-daemon not selected — set XMPP_BRIDGE_MODE=title-only in your")
        print("  shell profile or OpenCode environment to disable bridge traffic.")

    # Clean up legacy plugin copy if present
    if LEGACY_OPENCODE_PLUGIN_DST.is_file() or LEGACY_OPENCODE_PLUGIN_DST.is_symlink():
        LEGACY_OPENCODE_PLUGIN_DST.unlink()
        print(f"  Removed legacy plugin copy: {LEGACY_OPENCODE_PLUGIN_DST}")

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
    """Step: Install systemd user unit as a symlink."""
    print("\n--- bridge-daemon: systemd service ---")

    if not shutil.which("systemctl"):
        print("  systemctl not found, skipping")
        return True

    unit_src = _find_systemd_unit()
    if not unit_src:
        print("  Warning: systemd unit file not found, skipping")
        return True

    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    dst = SYSTEMD_DIR / "claude-xmpp-bridge.service"

    if (
        not upgrade
        and dst.exists()
        and not dst.is_symlink()
        and not yes_mode
        and not _confirm(f"    Replace {dst.name} with symlink?", default=True)
    ):
        return True

    if not upgrade and not dst.exists() and not _confirm("  Install systemd user service?", yes_mode=yes_mode):
        return True

    changed = _install_symlink(unit_src, dst)
    if changed:
        print(f"  systemd unit: {'updated' if upgrade else 'installed'} (symlink → {unit_src.resolve()})")
        print("  Run: systemctl --user daemon-reload")
    elif upgrade:
        print("  systemd unit: up to date")
    return True


def _step_sandbox(yes_mode: bool, upgrade: bool = False) -> bool:
    """Step: Install sandbox script and bash completion as symlinks."""
    print("\n--- sandbox: Sandbox script ---")

    sandbox_src = _find_sandbox_script()
    if not sandbox_src:
        print("  Warning: sandbox script source not found, skipping")
        return True

    if (
        not upgrade
        and SANDBOX_DST.exists()
        and not SANDBOX_DST.is_symlink()
        and not yes_mode
        and not _confirm("  Replace sandbox script with symlink?", default=True)
    ):
        pass  # user declined
    else:
        changed = _install_symlink(sandbox_src, SANDBOX_DST, make_executable=True)
        if changed:
            print(f"  sandbox: {'updated' if upgrade else 'installed'} (symlink → {sandbox_src.resolve()})")
        elif upgrade:
            print("  sandbox: up to date")

    # Install bash completion
    comp_src = _find_sandbox_completion()
    if comp_src:
        changed = _install_symlink(comp_src, SANDBOX_COMPLETION_DST)
        if changed:
            print(f"  bash completion: {'updated' if upgrade else 'installed'} (symlink → {comp_src.resolve()})")
        elif upgrade:
            print("  bash completion: up to date")

    return True


def _step_switches(yes_mode: bool) -> bool:
    """Step: Enable on/off switches."""
    print("\n--- bridge-daemon: Notification switches ---")

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


# ---------------------------------------------------------------------------
# Uninstall steps
# ---------------------------------------------------------------------------


def _uninstall_sandbox(yes_mode: bool) -> bool:
    """Remove sandbox script and bash completion (symlink or plain file)."""
    print("\n--- sandbox: Remove sandbox script ---")
    removed = 0
    for path in [SANDBOX_DST, SANDBOX_COMPLETION_DST]:
        if path.is_symlink() or path.is_file():
            kind = "symlink" if path.is_symlink() else "file"
            if yes_mode or _confirm(f"  Remove {path} ({kind})?", yes_mode=False):
                path.unlink()
                print(f"  Removed: {path}")
                removed += 1
            else:
                print(f"  Skipped: {path}")
        else:
            print(f"  Not found (already removed?): {path}")
    if not removed:
        print("  Nothing to remove.")
    return True


def _uninstall_hooks(yes_mode: bool) -> bool:
    """Remove installed hook scripts (symlinks or plain files) and clean up settings.json."""
    print("\n--- claude-hooks: Remove hook scripts ---")
    removed = 0
    for target_name in ALL_HOOK_TARGETS:
        dst = HOOKS_DIR / target_name
        if dst.is_symlink() or dst.is_file():
            kind = "symlink" if dst.is_symlink() else "file"
            if yes_mode or _confirm(f"  Remove {dst} ({kind})?", yes_mode=False):
                dst.unlink()
                print(f"  Removed: {dst}")
                removed += 1
            else:
                print(f"  Skipped: {dst}")
        else:
            print(f"  Not found: {dst}")
    if not removed:
        print("  No hook scripts to remove.")

    # Clean up settings.json
    if CLAUDE_SETTINGS.is_file():
        _uninstall_hooks_settings(yes_mode)

    return True


def _uninstall_hooks_settings(yes_mode: bool) -> None:
    """Remove managed hook event keys from ~/.claude/settings.json."""
    try:
        data = json.loads(CLAUDE_SETTINGS.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  Warning: could not read {CLAUDE_SETTINGS}: {e}", file=sys.stderr)
        return

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return

    to_remove = [k for k in MANAGED_HOOK_EVENTS if k in hooks]
    if not to_remove:
        print(f"  {CLAUDE_SETTINGS}: no managed hook events found")
        return

    print(f"  {CLAUDE_SETTINGS}: will remove events: {', '.join(to_remove)}")
    if not (yes_mode or _confirm("  Proceed?", yes_mode=False)):
        print("  Skipped settings.json update")
        return

    for key in to_remove:
        del hooks[key]

    if not hooks:
        del data["hooks"]

    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"  Updated: {CLAUDE_SETTINGS}")


def _uninstall_opencode(yes_mode: bool) -> bool:
    """Remove OpenCode plugin symlink/file and revert opencode.json permission config."""
    print("\n--- opencode-plugin: Remove OpenCode plugin ---")

    plugin_dst = OPENCODE_PLUGINS_DIR / "xmpp-bridge.js"
    if plugin_dst.is_symlink() or plugin_dst.is_file():
        kind = "symlink" if plugin_dst.is_symlink() else "file"
        if yes_mode or _confirm(f"  Remove {plugin_dst} ({kind})?", yes_mode=False):
            plugin_dst.unlink()
            print(f"  Removed: {plugin_dst}")
        else:
            print(f"  Skipped: {plugin_dst}")
    else:
        print(f"  Not found: {plugin_dst}")

    if LEGACY_OPENCODE_PLUGIN_DST.is_file() or LEGACY_OPENCODE_PLUGIN_DST.is_symlink():
        if yes_mode or _confirm(f"  Remove legacy plugin {LEGACY_OPENCODE_PLUGIN_DST}?", yes_mode=False):
            LEGACY_OPENCODE_PLUGIN_DST.unlink()
            print(f"  Removed: {LEGACY_OPENCODE_PLUGIN_DST}")
        else:
            print(f"  Skipped: {LEGACY_OPENCODE_PLUGIN_DST}")
    else:
        print(f"  Not found: {LEGACY_OPENCODE_PLUGIN_DST}")

    # Remove "permission" key from opencode.json if present
    if OPENCODE_SETTINGS.is_file():
        try:
            data = json.loads(OPENCODE_SETTINGS.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: could not read {OPENCODE_SETTINGS}: {e}", file=sys.stderr)
            return True

        if "permission" in data:
            print(f"  {OPENCODE_SETTINGS}: will remove 'permission' key")
            if yes_mode or _confirm("  Proceed?", yes_mode=False):
                del data["permission"]
                OPENCODE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
                print(f"  Updated: {OPENCODE_SETTINGS}")
            else:
                print("  Skipped opencode.json update")
        else:
            print(f"  {OPENCODE_SETTINGS}: 'permission' key not present")

    return True


def _uninstall_bridge(yes_mode: bool, purge: bool = False) -> bool:
    """Remove systemd service and optionally config directory."""
    print("\n--- bridge-daemon: Remove systemd service ---")

    unit_dst = SYSTEMD_DIR / "claude-xmpp-bridge.service"
    if unit_dst.is_symlink() or unit_dst.is_file():
        # Stop and disable service if systemctl available
        if shutil.which("systemctl"):
            try:
                subprocess.run(  # noqa: S603
                    ["systemctl", "--user", "disable", "--now", "claude-xmpp-bridge"],  # noqa: S607
                    check=False,
                    timeout=10,
                )
                print("  Stopped and disabled systemd service")
            except (subprocess.TimeoutExpired, OSError):
                pass
        kind = "symlink" if unit_dst.is_symlink() else "file"
        if yes_mode or _confirm(f"  Remove {unit_dst} ({kind})?", yes_mode=False):
            unit_dst.unlink()
            print(f"  Removed: {unit_dst}")
            if shutil.which("systemctl"):
                print("  Run: systemctl --user daemon-reload")
        else:
            print(f"  Skipped: {unit_dst}")
    else:
        print(f"  Not found: {unit_dst}")

    if purge:
        print("\n--- bridge-daemon: Remove configuration (--purge) ---")
        if CONFIG_DIR.is_dir():
            print(f"  Will remove: {CONFIG_DIR}")
            if yes_mode or _confirm("  Remove config directory (credentials, config.toml, token)?", yes_mode=False):
                shutil.rmtree(CONFIG_DIR)
                print(f"  Removed: {CONFIG_DIR}")
            else:
                print("  Skipped config directory")
        else:
            print(f"  Not found: {CONFIG_DIR}")

        # Remove switch files
        for switch in ["notify-enabled", "ask-enabled"]:
            path = SWITCHES_DIR / switch
            if path.is_file() and (yes_mode or _confirm(f"  Remove switch {switch}?", yes_mode=False)):
                path.unlink()
                print(f"  Removed: {path}")
    else:
        print(f"\n  Config directory kept: {CONFIG_DIR}")
        print("  (Use --purge to also remove credentials and config)")

    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


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
    parser.add_argument("--uninstall", action="store_true", help="Uninstall components (removes installed files)")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="With --uninstall: also remove credentials, config.toml and switch files",
    )
    parser.add_argument("--test-only", action="store_true", help="Only test XMPP connectivity")
    args = parser.parse_args()

    if args.test_only:
        ok = _step_test()
        sys.exit(0 if ok else 1)

    yes_mode = args.yes
    upgrade = args.upgrade

    print(f"claude-xmpp-bridge setup wizard v{__version__}")
    print("=" * 40)

    # --- UNINSTALL ---
    if args.uninstall:
        print("\nUninstall mode: select components to remove.")
        components = _ask_components(yes_mode=yes_mode)
        if not components:
            print("No components selected, nothing to do.")
            sys.exit(0)

        steps: list[tuple[str, Callable[[], bool]]] = []
        if COMPONENT_SANDBOX in components:
            steps.append(("sandbox", lambda: _uninstall_sandbox(yes_mode)))
        if COMPONENT_HOOKS in components:
            steps.append(("claude-hooks", lambda: _uninstall_hooks(yes_mode)))
        if COMPONENT_OPENCODE in components:
            steps.append(("opencode-plugin", lambda: _uninstall_opencode(yes_mode)))
        if COMPONENT_BRIDGE in components:
            steps.append(("bridge-daemon", lambda: _uninstall_bridge(yes_mode, purge=args.purge)))

        for name, step_fn in steps:
            try:
                step_fn()
            except KeyboardInterrupt:
                print("\n\nUninstall interrupted.")
                sys.exit(1)
            except Exception as e:
                print(f"\n  Error in {name}: {e}", file=sys.stderr)

        print("\n" + "=" * 40)
        print("Uninstall complete.")
        sys.exit(0)

    # --- UPGRADE ---
    if upgrade:
        print("\nUpgrade mode: updating managed files...")
        # In upgrade mode respect component selection too
        components = _ask_components(yes_mode=yes_mode)

        upgrade_steps: list[tuple[str, Callable[[], bool]]] = []
        if COMPONENT_HOOKS in components:
            _wb_upgrade = COMPONENT_BRIDGE in components

            def _upgrade_hooks(_wb: bool = _wb_upgrade) -> bool:
                return _step_hooks(yes_mode=True, upgrade=True, with_bridge=_wb)

            upgrade_steps.append(("hooks", _upgrade_hooks))
        if COMPONENT_OPENCODE in components:
            upgrade_steps.append(("opencode", lambda: _step_opencode(yes_mode=True, upgrade=True)))
        if COMPONENT_BRIDGE in components:
            upgrade_steps.append(("systemd", lambda: _step_systemd(yes_mode=True, upgrade=True)))
        if COMPONENT_SANDBOX in components:
            upgrade_steps.append(("sandbox", lambda: _step_sandbox(yes_mode=True, upgrade=True)))

        for name, step_fn in upgrade_steps:
            try:
                ok = step_fn()
            except KeyboardInterrupt:
                print("\n\nUpgrade interrupted.")
                sys.exit(1)
            except Exception as e:
                print(f"\n  Error in {name}: {e}", file=sys.stderr)
                ok = False
            if not ok and not _confirm(f"\n  Step '{name}' failed. Continue anyway?", yes_mode=True):
                sys.exit(1)

        print("\n" + "=" * 40)
        print("Upgrade complete!")
        sys.exit(0)

    # --- INSTALL ---
    print("\nSelect components to install.")
    components = _ask_components(yes_mode=yes_mode)

    if not components:
        print("No components selected, nothing to do.")
        sys.exit(0)

    install_steps: list[tuple[str, Callable[[], bool]]] = []

    # bridge-daemon must go first (credentials/config needed before hooks test)
    if COMPONENT_BRIDGE in components:
        install_steps.extend(
            [
                ("credentials", lambda: _step_credentials(yes_mode)),
                ("config", lambda: _step_config(yes_mode)),
                ("test", _step_test),
            ]
        )

    if COMPONENT_HOOKS in components:
        _wb_install = COMPONENT_BRIDGE in components

        def _install_hooks(_wb: bool = _wb_install) -> bool:
            return _step_hooks(yes_mode, with_bridge=_wb)

        install_steps.append(("hooks", _install_hooks))

    if COMPONENT_OPENCODE in components:
        _plugin_mode = PLUGIN_MODE_NORMAL if COMPONENT_BRIDGE in components else PLUGIN_MODE_TITLE_ONLY

        def _install_opencode(mode: str = _plugin_mode) -> bool:
            return _step_opencode(yes_mode, plugin_mode=mode)

        install_steps.append(("opencode", _install_opencode))

    if COMPONENT_BRIDGE in components:
        install_steps.extend(
            [
                ("systemd", lambda: _step_systemd(yes_mode)),
                ("switches", lambda: _step_switches(yes_mode)),
            ]
        )

    if COMPONENT_SANDBOX in components:
        install_steps.append(("sandbox", lambda: _step_sandbox(yes_mode)))

    for name, step_fn in install_steps:
        try:
            ok = step_fn()
        except KeyboardInterrupt:
            print("\n\nSetup interrupted.")
            sys.exit(1)
        except Exception as e:
            print(f"\n  Error in {name}: {e}", file=sys.stderr)
            ok = False

        if not ok and not _confirm(f"\n  Step '{name}' failed. Continue anyway?", yes_mode=yes_mode or upgrade):
            sys.exit(1)

    print("\n" + "=" * 40)
    print("Setup complete!")
    if COMPONENT_BRIDGE in components:
        print("\nNext steps:")
        print("  1. Start the bridge: claude-xmpp-bridge")
        print("  2. Or via systemd:   systemctl --user start claude-xmpp-bridge")
        print("  3. Test:             claude-xmpp-notify 'Hello from bridge!'")
    else:
        print("\nNote: XMPP bridge not installed.")
        print("  Hook scripts and OpenCode plugin will work without the bridge.")
        print("  To add the bridge later, run setup again and select bridge-daemon.")
