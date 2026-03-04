# OpenCode Plugin

This plugin integrates `claude-xmpp-bridge` with [OpenCode](https://opencode.ai).

> **Claude Code users:** see [`examples/hooks/`](../hooks/) for the equivalent Claude Code hook scripts.

## What it does

- On startup: renames the GNU Screen window to `🧠<project>` and registers the active session with the bridge
- `session.created` (e.g. `/new`): registers the new session
- `session.deleted`: unregisters the session from the bridge
- `session.idle`: sends the last assistant message via XMPP (switch: `notify-enabled`)
- `permission.ask`: blocking XMPP approval prompt — y/n/a (switch: `ask-enabled`)

## Setup

The easiest way is the setup wizard:

```bash
claude-xmpp-bridge-setup
```

The wizard installs the plugin to `~/.config/opencode/plugins/` and merges the permission config into `~/.config/opencode/opencode.json`.

### Manual setup

1. Copy the plugin:
   ```bash
   mkdir -p ~/.config/opencode/plugins
   cp plugins/xmpp-bridge.js ~/.config/opencode/plugins/
   ```

2. Merge `opencode.json` into `~/.config/opencode/opencode.json`:
   ```json
   {
     "permission": {
       "bash": "ask",
       "edit": "ask"
     }
   }
   ```

3. Enable notifications/permission requests (both disabled by default):
   ```bash
   mkdir -p ~/.config/xmpp-notify
   touch ~/.config/xmpp-notify/notify-enabled   # session.idle → XMPP message
   touch ~/.config/xmpp-notify/ask-enabled      # permission.ask → blocking XMPP prompt
   ```

## On/Off Switches

The same switch files as Claude Code hooks:

| File | Controls | Description |
|------|----------|-------------|
| `notify-enabled` | `session.idle` | Async — sends last assistant message via XMPP |
| `ask-enabled` | `permission.ask` | Sync — blocking approval prompt via XMPP |

Enable: `touch ~/.config/xmpp-notify/<file>`
Disable: `rm ~/.config/xmpp-notify/<file>`

## Coexistence with Claude Code

Claude Code and OpenCode sessions in the **same project directory coexist** — the bridge tracks them separately by `source`. Neither tool's session evicts the other's.

In `/list` output, OpenCode sessions are distinguished by the `🧠` prefix:

```
Sessions:
  /1 ~/projects/my-app [screen] *       ← Claude Code
  /2 ~/projects/my-app [🧠screen]       ← OpenCode
* = active session
```

## Dependencies

- `claude-xmpp-bridge` — must be running (via systemd or manually)
- `claude-xmpp-client` — socket client for bridge communication
- `claude-xmpp-ask` — used for blocking permission prompts
- GNU Screen or tmux
