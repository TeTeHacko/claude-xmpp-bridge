# claude-xmpp-bridge

XMPP bridge for [Claude Code](https://claude.ai/claude-code) — route messages between your Jabber/XMPP client and Claude Code sessions running in GNU Screen or tmux.

## Features

- **Persistent XMPP bot** — stays connected, routes messages to/from Claude sessions
- **Session management** — register multiple Claude sessions, switch between them
- **Multiplexer support** — GNU Screen and tmux backends
- **Permission requests** — approve/deny Claude actions via XMPP
- **Notifications** — receive task completions, errors, and other events
- **Configurable messages** — English default, easily translatable (Czech included)
- **SQLite persistence** — sessions survive bridge restarts
- **Secure** — credentials file permission checks, input validation, socket permissions

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

### Credentials

Create a credentials file with your XMPP bot password:

```bash
mkdir -p ~/.config/claude-xmpp-bridge
echo 'YOUR_XMPP_PASSWORD' > ~/.config/claude-xmpp-bridge/credentials
chmod 600 ~/.config/claude-xmpp-bridge/credentials
```

### Config file (optional)

Create `~/.config/claude-xmpp-bridge/config.toml`:

```toml
jid = "notify-bot@example.com"
recipient = "you@example.com"
# credentials = "~/.config/claude-xmpp-bridge/credentials"  # default
# socket_path = "~/.claude/bridge.sock"  # default
# db_path = "~/.claude/bridge.db"  # default
# messages_file = "/path/to/messages_cs.toml"  # optional
```

### Environment variables

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

## Custom messages

Copy `examples/messages_cs.toml` and customize:

```bash
claude-xmpp-bridge --messages /path/to/my_messages.toml
```

## Troubleshooting

### Bridge won't start

- **Missing credentials**: Ensure `~/.config/claude-xmpp-bridge/credentials` exists with `chmod 600`
- **Missing JID config**: Set `jid` and `recipient` via config file, environment variables, or CLI flags
- **Socket already in use**: A stale `~/.claude/bridge.sock` may remain after a crash — delete it manually

### Messages not delivered

- **XMPP auth failure**: Check JID and password; run with `--verbose` to see XMPP connection details
- **Firewall blocking XMPP**: Ensure ports 5222 (client) and 5269 (server) are open, or that DNS SRV records resolve
- **Connection timeout**: The bridge logs `XMPP connection timeout (30s)` — verify the server is reachable

### Socket permission denied

- The bridge socket (`~/.claude/bridge.sock`) is created with the user's umask
- Ensure the client runs as the same user as the bridge

### Verbose / quiet modes

```bash
# Debug logging (all components)
claude-xmpp-bridge --verbose

# Only warnings and errors
claude-xmpp-bridge --quiet

# Same flags work for notify and ask
claude-xmpp-notify --verbose "test"
```

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
