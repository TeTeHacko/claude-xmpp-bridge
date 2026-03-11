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
- `tool.execute.before`: reports agent state `running` to the bridge, but does not touch the Screen title (prevents redraw artefacts during active TUI rendering)

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

To avoid GNU Screen redraw artefacts, title updates are **debounced** and happen
only on coarse state transitions (`startup`, `session.created`, `session.status`,
`session.idle`, `permission.*`) — not on every tool call.

The debounce interval is configurable via `XMPP_BRIDGE_TITLE_DEBOUNCE_MS`
(default: `750`). Critical visual states still update immediately: `busy` turns
the state circle blue right away, and `permission.asked` turns it red right away.

When the bridge/MCP server is down, the plugin enters a temporary cooldown
(`XMPP_BRIDGE_RETRY_MS`, default: `60000`) and suppresses repeated bridge calls
instead of hammering `claude-xmpp-client` / MCP on every idle or state event.

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

## Agent State and Plugin Build

The plugin reports a build-aware `plugin_version` in the registration payload and keeps the bridge informed of agent state and active agent:

- **State**: `idle` — after registration and `session.idle`; `running` — when generating output or a tool starts
- **Agent**: emoji circle sent as `mode` field — updated when `message.updated` fires with a new agent name

This information appears in `/list` XMPP output as icons before the backend bracket and a compact build tag:

```
Sessions:
  /1  🧠🟠⏸  [screen #2]  @abc1234  ~/projects/my-app  *
  /2  🧠🔵▶  [screen #4]  @abc1234  ~/projects/other

* = active session
```

The plugin computes this ref from its own source file content, so local plugin-only
changes still show up in `/list` even if the Python package version stays the same.

Session context exposed through MCP now also includes `todos_version`, `todo_count`,
`lock_count`, and `inbox_count`, so agents can coordinate without shell-side state.

## MCP Inbox Polling

The plugin polls the MCP server (`http://127.0.0.1:7878`) for messages sent by other agents via `send_message` or `broadcast_message`. Polling happens:

- Immediately on each `session.idle` event
- Every 30 s while the session is idle

Received messages are injected into the terminal via `claude-xmpp-client relay`.
Inter-agent messages are wrapped by the bridge as a generated block with a JSON
metadata line, so it is immediately visible in shared Screen/tmux windows that
the text was injected by the bridge rather than typed manually.

When one agent wants a direct reply from another, it should call MCP
`send_message(..., sender_session_id=process.env.BRIDGE_SESSION_ID)`.
That causes the generated JSON metadata to include a non-null `from` session ID,
which the bridge remembers as `last_agent_sender` when the recipient drains its
inbox via `receive_messages(session_id)`. The receiving agent can then call
`reply_to_last_sender(session_id, message)` instead of manually copying the
session ID out of the relay metadata.

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
