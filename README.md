# claude-xmpp-bridge

[![CI](https://github.com/TeTeHacko/claude-xmpp-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/TeTeHacko/claude-xmpp-bridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

XMPP bridge for [Claude Code](https://claude.ai/claude-code) and [OpenCode](https://opencode.ai) — route messages between your Jabber/XMPP client and AI coding sessions running in GNU Screen or tmux.

## Quick Start

```bash
# 1. Install
pip install git+https://github.com/TeTeHacko/claude-xmpp-bridge.git

# 2. Run the setup wizard
claude-xmpp-bridge-setup

# 3. Start the bridge
systemctl --user start claude-xmpp-bridge

# 4. Enable on boot
systemctl --user enable claude-xmpp-bridge

# 5. Test
claude-xmpp-notify "Hello from bridge!"
```

## Features

- **Persistent XMPP bot** — stays connected, routes messages to/from coding sessions
- **Multi-tool support** — Claude Code and OpenCode can run simultaneously in the same project directory
- **Session management** — register multiple sessions, switch between them with `/1`, `/2`, …
- **Multiplexer support** — GNU Screen and tmux backends; reliable background-window delivery
- **Inter-agent communication** — agents relay messages to each other via socket commands (`relay`, `broadcast`) or MCP tools; all traffic is forwarded to the XMPP observer
- **MCP server** — exposes bridge as Model Context Protocol tools on port 7878 so agents can send/receive messages without screen relay
- **Agent state tracking** — agents report idle/running state; `/list` shows `⏸`/`▶` icons and plugin version
- **Permission notifications** — receive informative XMPP alerts when AI requests permission to run commands
- **Notifications** — receive task completions, errors, and other events with session icon + window ID prefix
- **Configurable messages** — English default, easily translatable (Czech, German, Polish, Slovak included)
- **SQLite persistence** — sessions survive bridge restarts; stable `/list` numbering across session restarts
- **Secure** — credentials file permission checks, input validation, socket permissions, optional socket token auth
- **Audit log** — structured JSON Lines event log for SIEM integration (journald or rotating file)
- **Setup wizard** — interactive `claude-xmpp-bridge-setup` configures everything including OpenCode

## System Dependencies

| Dependency | Required | Purpose |
|------------|----------|---------|
| Python 3.11+ | yes | Runtime |
| `jq` | yes | JSON processing in Claude Code hook scripts |
| GNU Screen or tmux | yes | Terminal multiplexer for message delivery |
| systemd (user) | optional | Service management |

## Installation

```bash
pip install git+https://github.com/TeTeHacko/claude-xmpp-bridge.git
```

Or install from source:

```bash
git clone https://github.com/TeTeHacko/claude-xmpp-bridge.git
cd claude-xmpp-bridge
pip install -e ".[dev]"
```

### Requirements

- Python 3.11+
- An XMPP account for the bot (e.g., `notify-bot@example.com`)
- GNU Screen or tmux
- `jq` — required by Claude Code hook scripts (see `examples/hooks/`)

## Configuration

### Setup wizard (recommended)

```bash
claude-xmpp-bridge-setup
```

The wizard walks through all configuration steps: credentials, config file, XMPP test, Claude Code hook installation, OpenCode plugin installation, systemd service, and notification switches.

Use `--test-only` to just verify XMPP connectivity:

```bash
claude-xmpp-bridge-setup --test-only
```

### Manual configuration

#### Credentials

Create a credentials file with your XMPP bot password:

```bash
mkdir -p ~/.config/claude-xmpp-bridge
echo 'YOUR_XMPP_PASSWORD' > ~/.config/claude-xmpp-bridge/credentials
chmod 600 ~/.config/claude-xmpp-bridge/credentials
```

#### Config file (optional)

Create `~/.config/claude-xmpp-bridge/config.toml`:

```toml
jid = "notify-bot@example.com"
recipient = "you@example.com"
# credentials = "~/.config/claude-xmpp-bridge/credentials"  # default
# socket_path = "~/.claude/bridge.sock"  # default
# db_path = "~/.claude/bridge.db"  # default
# messages_file = "/path/to/messages_cs.toml"  # optional
```

#### Environment variables

| Variable | Description |
|----------|-------------|
| `CLAUDE_XMPP_JID` | Bot XMPP JID |
| `CLAUDE_XMPP_RECIPIENT` | Your XMPP JID |
| `CLAUDE_XMPP_CREDENTIALS` | Path to credentials file |
| `CLAUDE_XMPP_SOCKET` | Unix socket path |
| `CLAUDE_XMPP_DB` | SQLite database path |
| `CLAUDE_XMPP_MESSAGES` | Messages TOML file path |
| `CLAUDE_XMPP_SOCKET_TOKEN` | Shared secret for socket authentication |
| `CLAUDE_XMPP_AUDIT_LOG` | Audit log destination (`journald` or file path) |
| `CLAUDE_XMPP_SMTP_HOST` | SMTP relay hostname or IP (empty = disabled) |
| `CLAUDE_XMPP_SMTP_PORT` | SMTP relay port (default: 25) |
| `CLAUDE_XMPP_EMAIL_THRESHOLD` | Email relay character threshold (default: 4000) |

Configuration priority: CLI flags > environment variables > config.toml > defaults.

### Socket token authentication

To prevent unauthorized local processes from interacting with the bridge socket, set a shared secret:

```toml
# config.toml
socket_token = "your-random-secret"
```

Or via environment variable:

```bash
export CLAUDE_XMPP_SOCKET_TOKEN="your-random-secret"
```

Or store it in a dedicated file (must be `chmod 600`):

```bash
echo 'your-random-secret' > ~/.config/claude-xmpp-bridge/socket_token
chmod 600 ~/.config/claude-xmpp-bridge/socket_token
```

The client reads the token from (in order of precedence): `CLAUDE_XMPP_SOCKET_TOKEN` env var → `~/.config/claude-xmpp-bridge/socket_token` file.
The token file **must** have `0600` permissions — the client exits with an error if the file is group/other-readable.

All hook scripts read this token from `CLAUDE_XMPP_SOCKET_TOKEN` automatically.

### Audit log

The bridge emits a structured JSON Lines event log suitable for SIEM integration:

```toml
# config.toml

# Write to systemd journal (default)
audit_log = "journald"

# Write to a rotating file (10 MB × 5 backups)
audit_log = "/var/log/claude-xmpp-bridge/audit.jsonl"
```

Each record is one JSON object per line:

```json
{"ts": "2026-03-05T14:32:01.123456Z", "event": "XMPP_IN", "from_jid": "user@example.com", "allowed": true, "body": "hello", "body_len": 5, "routed_to": "session"}
```

Audited events: `BRIDGE_START`, `BRIDGE_STOP`, `XMPP_IN`, `XMPP_OUT`, `XMPP_REJECTED`, `TOKEN_REJECTED`, `SESSION_REGISTERED`, `SESSION_REPLACED`, `SESSION_LIMIT_HIT`, `SESSION_UNREGISTERED`, `SESSION_EXPIRED`, `SESSION_STATE`, `TERMINAL_SEND`, `TERMINAL_SEND_FAILED`, `ASK_QUEUED`, `ASK_ANSWERED`, `ASK_TIMEOUT`, `RELAY_SENT`, `RELAY_FAILED`, `BROADCAST_SENT`, `MCP_SEND`, `MCP_BROADCAST`, `MCP_RECEIVE`, `SOCKET_CMD`.

### Email relay

When a notification exceeds `email_threshold` characters, the bridge sends the full
message body by email via a local SMTP relay and delivers only a truncated snippet
to XMPP.  This keeps your Jabber client responsive for very large payloads (full
file contents, long diffs, etc.).

```toml
# config.toml
smtp_host = "192.168.1.1"     # SMTP relay host (empty = disabled, default)
smtp_port = 25                # SMTP relay port (default: 25)
email_threshold = 4000        # trigger above this many chars (default: 4000)
```

| Env variable | Description |
|--------------|-------------|
| `CLAUDE_XMPP_SMTP_HOST` | SMTP relay hostname or IP |
| `CLAUDE_XMPP_SMTP_PORT` | SMTP relay port (default: 25) |
| `CLAUDE_XMPP_EMAIL_THRESHOLD` | Character threshold for email relay (default: 4000) |

Both sender and recipient are set to `recipient` (the bridge sends email to you
from itself).  The SMTP relay must accept unauthenticated connections from localhost.

Every outgoing XMPP message is recorded as a `XMPP_OUT` audit event with
`email_relay: true/false` so you can diagnose delivery in `journalctl`.

## Usage

### Start the bridge

```bash
claude-xmpp-bridge --jid notify-bot@example.com --recipient you@example.com
```

Or with a config file, just:

```bash
claude-xmpp-bridge
```

### XMPP commands

Send these from your Jabber client to the bot:

| Command | Description |
|---------|-------------|
| `/list` or `/l` | List active sessions with state (`⏸` idle / `▶` running) and plugin version |
| `/N message` | Send message to session #N |
| `/help` | Show help |
| _plain text_ | Send to last active session |

Sessions are shown with a tool tag: `[screen]`, `[tmux]`, `[🧠screen]` (OpenCode), `[read-only]`.
The active session is marked with `*`.

### Standalone tools

```bash
# Send a notification (direct XMPP, no bridge needed)
claude-xmpp-notify "Build completed"
echo "Pipeline failed" | claude-xmpp-notify

# Send and wait for reply
# When the bridge is running, ask is routed through it (FIFO queue, no extra XMPP connection).
# Falls back to a direct XMPP connection when the bridge is not running.
reply=$(claude-xmpp-ask "Deploy to production? (y/n)" --timeout 300)

# Client (communicates with running bridge)
claude-xmpp-client send "Hello"
claude-xmpp-client register '{"session_id":"abc","sty":"12345.pts-0","window":"0","project":"/home/user/project","backend":"screen"}'
claude-xmpp-client unregister abc
claude-xmpp-client notify '{"session_id":"abc","message":"Task completed"}'
claude-xmpp-client ping                             # exit 0 if bridge is running, 1 otherwise
claude-xmpp-client list                             # list all registered sessions as JSON
claude-xmpp-client relay --to SESSION_ID "message"  # send message to a specific session
claude-xmpp-client broadcast --session-id SELF_ID "message"  # send to all other sessions
claude-xmpp-client state '{"session_id":"abc","state":"idle"}'  # report agent state
```

### systemd service

```bash
cp examples/systemd/claude-xmpp-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-xmpp-bridge
```

## Inter-agent Communication

Multiple AI agents running in different sessions can communicate with each other through the bridge. All inter-agent traffic is forwarded to the human observer's XMPP client as structured JSON:

```json
{"type": "relay", "mode": "nudge", "from": "sender_id", "to": "target_id", "message": "...", "ts": 1741612800.123}
{"type": "broadcast", "mode": "screen", "from": "sender_id", "to": ["id1", "id2"], "message": "...", "ts": 1741612800.123}
```

MCP server relay messages also include a `"message_id"` field. All other XMPP messages (notify, ask, response, system) remain plain text.

### Socket protocol

```bash
# Send a message to one specific session (by session_id, index, or project path prefix)
claude-xmpp-client relay --to SESSION_ID "message"

# Send a message to all other sessions
claude-xmpp-client broadcast --session-id MY_SESSION_ID "message"

# List all registered sessions (returns JSON)
claude-xmpp-client list

# Report agent state (idle / running)
claude-xmpp-client state '{"session_id":"abc","state":"idle"}'
```

### MCP server (port 7878)

The bridge also exposes an HTTP MCP server on port 7878 (streamable-HTTP transport). Agents with MCP tool access can use it without any shell commands:

| Tool | Description |
|------|-------------|
| `send_message(to, message, screen=True, nudge=False)` | Deliver a message to a session; `screen=False` enqueues to inbox only; `nudge=True` sends only a CR to wake the agent (message stored in inbox, delivered on next `session.idle`) |
| `broadcast_message(message, sender_session_id)` | Deliver to all sessions except sender |
| `receive_messages(session_id)` | Drain inbox — returns messages sent to this session |
| `list_sessions()` | Enumerate all sessions with metadata, state, plugin version, sty, and window |

Configure the MCP port:

```toml
# config.toml
mcp_port = 7878   # set to 0 to disable
```

Or via CLI / env:

```bash
claude-xmpp-bridge --mcp-port 7878
CLAUDE_XMPP_MCP_PORT=7878 claude-xmpp-bridge
```

## Claude Code Integration

See [`examples/hooks/`](examples/hooks/) for Claude Code hook configurations that automatically:

- Register/unregister sessions on start/stop
- Forward notifications via XMPP
- Ask for permission approval via XMPP
- Send task completion notices

### Hook Input JSON Schemas

Each hook receives JSON on stdin. Here are the fields available per event:

#### SessionStart

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project"
}
```

#### SessionEnd

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project"
}
```

#### Notification

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "notification_type": "tool_error",
  "title": "Error",
  "message": "Command failed with exit code 1"
}
```

#### PermissionRequest

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

#### TaskCompleted

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "task_subject": "Fix authentication bug"
}
```

#### Stop

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "last_assistant_message": "Done! All tests pass."
}
```

## OpenCode Integration

See [`examples/opencode/`](examples/opencode/) for an OpenCode plugin that provides the same functionality as the Claude Code hooks:

- Renames the GNU Screen/tmux window with traffic-light state indicator on startup
- Registers/unregisters sessions automatically
- Sends last assistant message via XMPP on `session.idle` (switch: `notify-enabled`)
- Sends informative XMPP notification when AI requests permission (`permission.asked`) — approval still happens in the TUI (switch: `ask-enabled`)
- Reports agent state (`idle`/`running`) to the bridge for `/list` display
- Polls MCP inbox on `session.idle` and every 30 s — injects pending inter-agent messages into the session

Window title traffic-light states:

| Title | Meaning |
|-------|---------|
| `🧠🟢 project` | idle — waiting for input |
| `🧠🔵 project` | running — model generating output |
| `🧠🔴 project` | requires interaction — permission dialog open in TUI |

### Install

The setup wizard handles installation automatically (Step 5):

```bash
claude-xmpp-bridge-setup
```

Or manually:

```bash
mkdir -p ~/.config/opencode/plugins
cp examples/opencode/plugins/xmpp-bridge.js ~/.config/opencode/plugins/
```

Merge the permission config into `~/.config/opencode/opencode.json`:

```json
{
  "permission": {
    "bash": "ask",
    "edit": "ask"
  }
}
```

### Coexistence with Claude Code

Claude Code and OpenCode sessions in the **same project directory coexist** — the bridge tracks them separately and neither evicts the other. In `/list` output, OpenCode sessions are distinguished by the `🧠` prefix (e.g., `[🧠screen]`).

### Session registration payload

The OpenCode plugin registers sessions with `source: "opencode"` and reports its plugin version:

```json
{
  "session_id": "ses_abc123_w4",
  "sty": "12345.pts-0.hostname",
  "window": "4",
  "project": "/home/user/project",
  "backend": "screen",
  "source": "opencode",
  "plugin_version": "0.7.16"
}
```

### Agent identity environment variables

After registration, the plugin exports the following variables to `process.env`, which are inherited by all bash tools the agent spawns:

| Variable | Example value | Description |
|----------|--------------|-------------|
| `BRIDGE_SESSION_ID` | `ses_abc123_w4` | Full bridge session ID (with `_wN` suffix) |
| `BRIDGE_WINDOW` | `4` | Screen window number (reliable, read from `/proc/ppid/environ`) |
| `WINDOW` | `4` | Corrected `$WINDOW` (overrides any wrong inherited value) |

Agents can use `env | grep BRIDGE_SESSION` in a bash tool to discover their own identity.

## Security Sandbox (Optional)

To further secure your AI coding sessions against accessing sensitive files in your home directory (e.g., `~/.ssh`, `~/.aws`), you can run Claude Code or OpenCode inside a restricted **Bubblewrap sandbox**.

See [`examples/sandbox/README.md`](examples/sandbox/README.md) for details. The `claude-xmpp-bridge-setup` wizard offers to install this wrapper automatically.

## Custom messages

Copy one of the example locale files and customize:

```bash
claude-xmpp-bridge --messages /path/to/messages_cs.toml
```

Available locales: `en` (default), `cs`, `de`, `pl`, `sk`.

## Debugging

### Check bridge status

```bash
systemctl --user status claude-xmpp-bridge
```

### View logs

```bash
journalctl --user -u claude-xmpp-bridge -f
```

### Test XMPP connectivity

```bash
claude-xmpp-bridge-setup --test-only
```

### Verbose logging

```bash
# Debug logging (all components)
claude-xmpp-bridge --verbose

# Same for notify and ask
claude-xmpp-notify --verbose "test"
```

### Inspect socket communication

```bash
echo '{"cmd":"send","message":"test"}' | socat - UNIX-CONNECT:~/.claude/bridge.sock
```

### Common issues

- **Bridge won't start — "already running"**: A stale socket may remain after a crash. Remove it: `rm ~/.claude/bridge.sock`
- **Missing credentials**: Run `claude-xmpp-bridge-setup` or create `~/.config/claude-xmpp-bridge/credentials` manually
- **XMPP auth failure**: Check JID and password; run with `--verbose` to see connection details
- **Messages not delivered**: Ensure ports 5222/5269 are open, or that DNS SRV records resolve
- **Socket permission denied**: Ensure the client runs as the same user as the bridge
- **Text appears in Claude Code prompt but Enter is not submitted**: Upgrade to the latest version — a regression in GNU Screen message delivery (using `paste` instead of `stuff`) was fixed

## Development

```bash
git clone https://github.com/TeTeHacko/claude-xmpp-bridge.git
cd claude-xmpp-bridge
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=src/claude_xmpp_bridge --cov-report=term-missing

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

CI runs automatically on every push and pull request via GitHub Actions
(`.github/workflows/ci.yml`), testing Python 3.11, 3.12 and 3.13.

## License

MIT
