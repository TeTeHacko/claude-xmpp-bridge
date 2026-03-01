# Claude Code Hooks Examples

These are example hook configurations for integrating `claude-xmpp-bridge` with [Claude Code](https://claude.ai/claude-code).

## Setup

1. Copy `settings.json` to `~/.claude/settings.json` (or merge with your existing config)
2. Copy hook scripts to `~/.claude/hooks/` and make them executable:
   ```bash
   mkdir -p ~/.claude/hooks
   cp permission-ask.sh ~/.claude/hooks/permission-ask-xmpp.sh
   cp format-location.sh ~/.claude/hooks/format-location.sh
   chmod +x ~/.claude/hooks/permission-ask-xmpp.sh ~/.claude/hooks/format-location.sh
   ```
3. Enable hooks (both disabled by default):
   ```bash
   mkdir -p ~/.config/xmpp-notify
   touch ~/.config/xmpp-notify/ask-enabled      # permission requests via XMPP
   touch ~/.config/xmpp-notify/notify-enabled    # notifications, task completed, stop
   ```

## On/Off Switches

Hooks are controlled by files in `~/.config/xmpp-notify/`:

| File | Controls | Description |
|------|----------|-------------|
| `ask-enabled` | PermissionRequest | Sync — asks for approval via XMPP, shows command detail |
| `notify-enabled` | Notification, TaskCompleted, Stop | Async — forwards status messages via XMPP |

Enable: `touch ~/.config/xmpp-notify/<file>`
Disable: `rm ~/.config/xmpp-notify/<file>`

## Hooks Overview

| Hook | Type | Description |
|------|------|-------------|
| **SessionStart** | sync | Sets GNU Screen window title |
| **SessionStart** | async | Registers session with bridge |
| **SessionEnd** | async | Unregisters session from bridge |
| **Notification** | async | Forwards notifications via XMPP (switch: `notify-enabled`) |
| **PermissionRequest** | sync | Asks for approval via XMPP with command detail (switch: `ask-enabled`) |
| **TaskCompleted** | async | Sends task completion notice (switch: `notify-enabled`) |
| **Stop** | async | Sends last assistant message (switch: `notify-enabled`) |

## Helper Scripts

- **`format-location.sh`** — Formats session location for notifications.
  Queries the bridge for the registered project via `claude-xmpp-client query`.
  Shows `project → cwd` when working in a different directory, or just `cwd` when same.
- **`permission-ask.sh`** — Permission request handler.
  Shows tool name, description, and command. For Bash: description + `$ command`.
  For Edit: file path + old_string preview. Timeout: 300s.

## Dependencies

- `jq` — for JSON processing in shell hooks
- `claude-xmpp-bridge` — must be running (via systemd or manually)
- `claude-xmpp-ask` — used by the permission hook
- `claude-xmpp-client` — socket client for bridge communication
