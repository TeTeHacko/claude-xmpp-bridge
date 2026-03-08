# OpenCode Plugin

This plugin integrates `claude-xmpp-bridge` with [OpenCode](https://opencode.ai).

> **Claude Code users:** see [`examples/hooks/`](../hooks/) for the equivalent Claude Code hook scripts.

## What it does

- On startup: renames the GNU Screen/tmux window to `🧠📋🟢<project>` and registers the active session with the bridge
- `session.created` (e.g. `/new`): registers the new session
- `session.deleted`: unregisters the session from the bridge
- `session.idle`:
  - sends the last assistant message via XMPP (switch: `notify-enabled`)
  - polls MCP inbox for pending inter-agent messages and injects them into the session
  - reports agent state `idle` to the bridge
- `permission.asked`: sends an informative XMPP notification showing what the AI wants to run — the actual approval/denial still happens in the OpenCode TUI (switch: `ask-enabled`)
- `permission.replied`: sets title to `🧠🔵` (model continues after permission)
- Reports agent state `running` when the model starts generating output
- `tool.execute.before`: detects the tool being executed and updates the mode icon in the window title immediately

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

3. Enable notifications/permission alerts (both disabled by default):
   ```bash
   mkdir -p ~/.config/xmpp-notify
   touch ~/.config/xmpp-notify/notify-enabled   # session.idle → XMPP message
   touch ~/.config/xmpp-notify/ask-enabled      # permission.asked → XMPP notification
   ```

## On/Off Switches

The same switch files as Claude Code hooks:

| File | Controls | Description |
|------|----------|-------------|
| `notify-enabled` | `session.idle` | Sends last assistant message via XMPP |
| `ask-enabled` | `permission.asked` | Sends informative XMPP notification about pending permission |

Enable: `touch ~/.config/xmpp-notify/<file>`
Disable: `rm ~/.config/xmpp-notify/<file>`

## Window Title — Mode + State

The plugin sets the GNU Screen/tmux window title with two icons: a **mode icon** (what the agent is doing) and a **state circle** (whether it is active):

### Mode icons

| Icon | Mode | When |
|------|------|------|
| `📋` | planning | default — reading files, searching, thinking |
| `✏️` | code | editing files (`edit`, `write`, `multiedit` tools) |
| `⚙️` | build | running commands (`bash` tool) |

Mode resets to `📋` at the start of each new response (`session.status: busy`).

Mode icons are configurable via environment variables: `BRIDGE_MODE_PLANNING`, `BRIDGE_MODE_CODE`, `BRIDGE_MODE_BUILD`.

### State circles

| Icon | State | When |
|------|-------|------|
| `🟢` | idle | startup, `session.idle`, `/new` |
| `🔵` | running | model generating output, after permission confirmed |
| `🔴` | requires interaction | permission dialog open in TUI — needs your input |

### Example titles

```
🧠📋🟢 my-project    ← idle, planning mode (just started or finished)
🧠✏️🔵 my-project    ← running, editing files
🧠⚙️🔵 my-project    ← running, executing bash command
🧠⚙️🔴 my-project    ← permission required for bash
```

## Agent State, Mode, and Plugin Version

The plugin reports its version (`plugin_version`) in the registration payload and keeps the bridge informed of agent state and mode:

- **State**: `idle` — after registration and `session.idle`; `running` — when generating output
- **Mode**: `planning` / `code` / `build` — updated before each tool call

This information appears in `/list` XMPP output as icons before the backend bracket and a version tag:

```
Sessions:
  /1  🧠📋⏸  [screen #2]  v0.7.18  ~/projects/my-app  *
  /2  🧠✏️▶  [screen #4]  v0.7.18  ~/projects/other

* = active session
```

## MCP Inbox Polling

The plugin polls the MCP server (`http://127.0.0.1:7878`) for messages sent by other agents via `send_message` or `broadcast_message`. Polling happens:

- Immediately on each `session.idle` event
- Every 30 s while the session is idle

Received messages are injected into the terminal via `claude-xmpp-client relay`.

## Coexistence with Claude Code

Claude Code and OpenCode sessions in the **same project directory coexist** — the bridge tracks them separately by `source`. Neither tool's session evicts the other's.

In `/list` output, OpenCode sessions are distinguished by the `🧠` prefix (Claude Code uses `⚡`):

```
Sessions:
  /1  ⚡⏸   [screen #0]  ~/projects/my-app  *    ← Claude Code
  /2  🧠📋⏸  [screen #2]  ~/projects/my-app       ← OpenCode

* = active session
```

## Dependencies

- `claude-xmpp-bridge` — must be running (via systemd or manually)
- `claude-xmpp-client` — socket client for bridge communication (relay, state, register, unregister, notify)
- GNU Screen or tmux

