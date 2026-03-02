# claude-xmpp-bridge

XMPP bridge for [Claude Code](https://claude.ai/claude-code) — route messages between your Jabber/XMPP client and Claude Code sessions running in GNU Screen or tmux.

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

- **Persistent XMPP bot** — stays connected, routes messages to/from Claude sessions
- **Session management** — register multiple Claude sessions, switch between them
- **Multiplexer support** — GNU Screen and tmux backends
- **Permission requests** — approve/deny Claude actions via XMPP
- **Notifications** — receive task completions, errors, and other events
- **Configurable messages** — English default, easily translatable (Czech included)
- **SQLite persistence** — sessions survive bridge restarts
- **Secure** — credentials file permission checks, input validation, socket permissions
- **Setup wizard** — interactive `claude-xmpp-bridge-setup` configures everything

## System Dependencies

| Dependency | Required | Purpose |
|------------|----------|---------|
| Python 3.11+ | yes | Runtime |
| `jq` | yes | JSON processing in hook scripts |
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

The wizard walks through all configuration steps: credentials, config file, XMPP test, hook installation, systemd service, and notification switches.

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

Configuration priority: CLI flags > environment variables > config.toml > defaults.

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
| `/list` or `/l` | List active Claude sessions |
| `/N message` | Send message to session #N |
| `/help` | Show help |
| _plain text_ | Send to last active session |

### Standalone tools

```bash
# Send a notification
claude-xmpp-notify "Build completed"
echo "Pipeline failed" | claude-xmpp-notify

# Send and wait for reply
reply=$(claude-xmpp-ask "Deploy to production? (y/n)" --timeout 300)

# Client (communicates with running bridge, falls back to notify)
claude-xmpp-client send "Hello"
claude-xmpp-client register '{"session_id":"abc","sty":"12345.pts-0","window":"0","project":"/home/user/project","backend":"screen"}'
claude-xmpp-client unregister abc
```

### systemd service

```bash
cp examples/systemd/claude-xmpp-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-xmpp-bridge
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

## Custom messages

Copy `examples/messages_cs.toml` and customize:

```bash
claude-xmpp-bridge --messages /path/to/my_messages.toml
```

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

## Development

```bash
git clone https://github.com/TeTeHacko/claude-xmpp-bridge.git
cd claude-xmpp-bridge
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

## License

MIT
