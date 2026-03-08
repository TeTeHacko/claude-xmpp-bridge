# OpenCode Plugin

This plugin integrates `claude-xmpp-bridge` with [OpenCode](https://opencode.ai).

> **Claude Code users:** see [`examples/hooks/`](../hooks/) for the equivalent Claude Code hook scripts.

## What it does

- On startup: renames the GNU Screen/tmux window to `⚪🟢<project>` and registers the active session with the bridge
- `session.created` (e.g. `/new`): registers the new session, resets agent icon to ⚪
- `session.deleted`: unregisters the session from the bridge
- `session.idle`:
  - sends the last assistant message via XMPP (switch: `notify-enabled`)
  - polls MCP inbox for pending inter-agent messages and injects them into the session
  - reports agent state `idle` to the bridge
- `message.updated`: detects the active agent from `info.agent` field and updates the agent circle icon in the window title
- `permission.asked`: sends an informative XMPP notification showing what the AI wants to run — the actual approval/denial still happens in the OpenCode TUI (switch: `ask-enabled`)
- `permission.replied`: sets title to `{agent}🔵` (model continues after permission)
- Reports agent state `running` when the model starts generating output
- `tool.execute.before`: updates the state circle to 🔵 immediately before each tool call

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

## Window Title — Agent + State

The plugin sets the GNU Screen/tmux window title with two icons: an **agent circle** (which agent is active) and a **state circle** (whether it is running):

### Agent circles

Each circle colour matches the agent's colour in the OpenCode TUI:

| Icon | Agent | TUI colour | When |
|------|-------|-----------|------|
| `⚪` | unknown | — | startup, after `/new`, before first response |
| `🔵` | `build` | secondary (blue) | default built-in agent |
| `🟣` | `plan` | accent (purple) | planning/read-only agent |
| `🟠` | `coder` | primary (orange) | custom coding agent |
| `🩵` | `local` | info (cyan) | custom local Ollama agent |

Agent is detected from `message.updated` events — the only reliable server-side signal (Tab-switching is client-side only, with no server event).

Icons are configurable via environment variables `BRIDGE_AGENT_<NAME>` (uppercase agent name):
```bash
export BRIDGE_AGENT_BUILD=🔵
export BRIDGE_AGENT_PLAN=🟣
export BRIDGE_AGENT_CODER=🟠
export BRIDGE_AGENT_LOCAL=🩵
```

### State circles

| Icon | State | When |
|------|-------|------|
| `🟢` | idle | startup, `session.idle`, `/new` |
| `🔵` | running | model generating output, after permission confirmed |
| `🔴` | requires interaction | permission dialog open in TUI — needs your input |

### Example titles

```
⚪🟢 my-project    ← idle, agent not yet known (just started or /new)
🟠🔵 my-project    ← coder agent running
🔵🟢 my-project    ← build agent idle
🟣🔴 my-project    ← plan agent, permission required
```

## Agent State and Plugin Version

The plugin reports its version (`plugin_version`) in the registration payload and keeps the bridge informed of agent state and active agent:

- **State**: `idle` — after registration and `session.idle`; `running` — when generating output
- **Agent**: emoji circle sent as `mode` field — updated when `message.updated` fires with a new agent name

This information appears in `/list` XMPP output as icons before the backend bracket and a version tag:

```
Sessions:
  /1  🧠🟠⏸  [screen #2]  v0.7.19  ~/projects/my-app  *
  /2  🧠🔵▶  [screen #4]  v0.7.19  ~/projects/other

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
  /1  ⚡⏸    [screen #0]  ~/projects/my-app  *    ← Claude Code
  /2  🧠🟠⏸  [screen #2]  ~/projects/my-app       ← OpenCode (coder agent)

* = active session
```

## Dependencies

- `claude-xmpp-bridge` — must be running (via systemd or manually)
- `claude-xmpp-client` — socket client for bridge communication (relay, state, register, unregister, notify)
- GNU Screen or tmux
