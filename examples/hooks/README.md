# Claude Code Hooks

These hook scripts integrate `claude-xmpp-bridge` with [Claude Code](https://claude.ai/claude-code).

> **OpenCode users:** see [`examples/opencode/`](../opencode/) for the equivalent OpenCode plugin.

## Setup

The easiest way is the setup wizard:

```bash
claude-xmpp-bridge-setup
```

### Manual setup

1. Copy hook scripts to `~/.claude/hooks/` and make them executable:
   ```bash
   mkdir -p ~/.claude/hooks
   cp session-start-title.sh session-start-register.sh session-end.sh \
      notification.sh task-completed.sh stop.sh \
      format-location.sh ~/.claude/hooks/
   cp permission-ask.sh ~/.claude/hooks/permission-ask-xmpp.sh
   chmod +x ~/.claude/hooks/*.sh
   ```
2. Copy `settings.json` to `~/.claude/settings.json` (or merge with your existing config)
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

## Hook Scripts

| Script | Hook Event | Type | Description |
|--------|------------|------|-------------|
| `session-start-title.sh` | SessionStart | sync | Sets GNU Screen window title |
| `session-start-register.sh` | SessionStart | async | Registers session with bridge |
| `session-end.sh` | SessionEnd | async | Unregisters session from bridge |
| `notification.sh` | Notification | async | Forwards notifications via XMPP (switch: `notify-enabled`) |
| `permission-ask.sh` | PermissionRequest | sync | Asks for approval via XMPP with command detail (switch: `ask-enabled`) |
| `task-completed.sh` | TaskCompleted | async | Sends task completion notice (switch: `notify-enabled`) |
| `stop.sh` | Stop | async | Sends last assistant message (switch: `notify-enabled`) |

## Hook Input JSON Schemas

Each hook receives JSON on stdin. These are the fields available for each event:

### SessionStart

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project"
}
```

### SessionEnd

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project"
}
```

### Notification

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "notification_type": "tool_error",
  "title": "Error",
  "message": "Command failed with exit code 1"
}
```

### PermissionRequest

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "tool_name": "Bash",
  "tool_input": {
    "command": "rm -rf /tmp/build",
    "description": "Clean build directory"
  },
  "permission_suggestions": [
    {"type": "allow_tool", "tool_name": "Bash", "prefix": "rm -rf /tmp/build"}
  ]
}
```

### TaskCompleted

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "task_subject": "Fix authentication bug"
}
```

### Stop

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "last_assistant_message": "Done! All tests pass."
}
```

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
