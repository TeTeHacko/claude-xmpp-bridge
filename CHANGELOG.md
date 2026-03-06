# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-03-06

### Fixed
- Sandbox: bind-mount `/dev/tty` from host into the sandbox so that processes
  inside (e.g. OpenCode plugin) can write ANSI escape sequences for title
  management — `--dev` creates a fresh devtmpfs that does not include `/dev/tty`
- OpenCode plugin: redirect `printf` title output explicitly to `>/dev/tty
  2>/dev/null` so it reaches the terminal even when the subprocess stdout is
  not a tty

## [0.3.0] - 2026-03-06

### Added
- Setup wizard: modular component selection — interactive toggle menu lets the
  user choose which components to install: `sandbox`, `claude-hooks`,
  `opencode-plugin`, `bridge-daemon`; all selected by default
- Setup wizard: `--uninstall` flag — removes installed files for selected
  components; removes managed hook event keys from `~/.claude/settings.json`
  and `permission` key from `~/.config/opencode/opencode.json`
- Setup wizard: `--uninstall --purge` also removes credentials, `config.toml`,
  `socket_token` and notification switch files
- Setup wizard: `claude-hooks` without `bridge-daemon` installs only
  `session-start-title.sh` (title management works without bridge)
- OpenCode plugin: runtime bridge detection via `claude-xmpp-client ping` at
  startup — title management always works; XMPP register/unregister/notify/
  response only active when bridge daemon is running
- Bridge daemon + client: `ping` command — `claude-xmpp-client ping` exits 0
  if bridge is running, 1 otherwise

### Changed
- Setup wizard: Mode 1 / Mode 2 selection replaced by modular component
  toggle menu; `--upgrade` also respects component selection
- `bridge-daemon` steps (credentials, config, systemd, switches) now run
  before hooks/opencode to ensure config is available during install

## [0.2.11] - 2025-03-05

### Fixed
- OpenCode plugin: restore `screen -X title` as primary title method (works
  outside sandbox); fall back to ANSI escape on `/dev/tty` only when Screen
  socket is unavailable (inside sandbox or no `$STY`)

## [0.2.10] - 2025-03-05

### Fixed
- Sandbox: set window title (`⚡`/`🧠` + project name) via ANSI escape
  sequences written to `/dev/tty` before launching bwrap — the wrapper
  script has a controlling terminal unlike hook subprocesses; restore title
  to bare project name on exit via `trap EXIT`; detect icon from command
  (`claude` → `⚡`, `opencode` → `🧠`)
- Sandbox: replace `exec bwrap` with plain `bwrap` so the `trap EXIT`
  title-restore handler runs after the sandbox exits

## [0.2.9] - 2025-03-05

### Fixed
- `session-start-title.sh`: use ANSI escape sequences (`\033k...\033\\` for
  Screen, `\033]2;...\007` for xterm/tmux) instead of `screen -X title` —
  works inside bubblewrap sandbox without screen socket access; adds `⚡` icon;
  fixes `WINDOW: unbound variable` crash with `set -uo pipefail`
- OpenCode plugin: replace `screen -X title` with ANSI escape sequences in
  `setTitle()` — works inside sandbox; removes dependency on screen socket;
  `setTitle` is now synchronous
- Sandbox: mount `~/.local/bin` read-only and add it to `PATH` so
  `claude-xmpp-client` and other pipx tools are accessible from hooks, and
  Claude Code's `installMethod=native` self-check passes

## [0.2.8] - 2025-03-05

### Fixed
- Sandbox: propagate `$STY`, `$WINDOW`, `$TMUX`, `$TMUX_PANE` into the
  sandbox environment so Claude Code hook `session-start-title.sh` doesn't
  fail with `STY: unbound variable` and session registration works correctly

## [0.2.7] - 2025-03-05

### Fixed
- Sandbox: mount `~/.claude.json` (RW) so Claude Code sees the logged-in
  account and skips the onboarding wizard (the file lives directly in $HOME,
  not inside ~/.claude/, so it was previously hidden by the tmpfs base)

## [0.2.6] - 2025-03-05

### Fixed
- Sandbox: mount `~/.local/share/opencode` and `~/.local/state/opencode` (RW)
  so OpenCode sessions, auth, and prompt history are visible inside the sandbox
- Sandbox: mount `~/.config/claude-xmpp-bridge` and `~/.config/xmpp-notify` (RO)
  so bridge hooks can read socket token and credentials inside the sandbox

## [0.2.5] - 2025-03-05

### Added
- Setup wizard `--upgrade` / `-u` flag: updates managed files (hooks, plugins,
  sandbox, systemd unit) without interactive prompts — overwrites only changed
  files, skips identical ones with "up to date" status

## [0.2.4] - 2025-03-05

### Security
- Sandbox: add `--new-session` to prevent reading `/proc/[pid]/environ`
  of host processes (PID namespace isolation hardening)
- Sandbox: add `--hostname sandbox` to hide the real hostname inside
  the sandbox (UTS namespace was unshared but hostname was inherited)

## [0.2.3] - 2025-03-05

### Added
- Bash completion for `sandbox` script — completes options, SSH key names,
  Kubernetes contexts, filesystem paths, and commands from `$PATH`
- Setup wizard installs completion to `~/.local/share/bash-completion/completions/sandbox`

## [0.2.2] - 2025-03-05

### Fixed
- Fix shared-data installation paths in wheel: setup wizard could not find
  hook scripts, OpenCode plugin, sandbox script, or systemd unit when installed
  via pip/pipx (missing `share/` prefix in hatchling shared-data targets)

## [0.2.1] - 2025-03-05

### Security
- Set `0600` permissions on `config.toml` after creation in setup wizard
  (previously retained default umask, exposing `socket_token` to other users)
- Escape TOML special characters in user-supplied JID/recipient during setup
  to prevent TOML injection
- Use `hmac.compare_digest()` for constant-time socket token comparison

### Changed
- Add `[tool.coverage]` configuration to `pyproject.toml`
- Add docstrings to key bridge methods (`_on_xmpp_message`, `_handle_request`,
  `_handle_register`)

### Fixed
- Use `__version__` import in test_cli.py instead of hardcoded version string
- Remove unused `config_toml` fixture from test conftest

## [0.2.0] - 2025-02-15

### Added
- OpenCode integration with JS plugin and `opencode.json` permission config
- Source icons: configurable per-source icons via `[source_icons]` TOML section
- No-backend session TTL (24h automatic expiry)
- Audit logging with journald and rotating file backends
- Interactive setup wizard (`claude-xmpp-bridge-setup`)
- Bubblewrap sandbox script for filesystem isolation
- Configurable UI messages with TOML override and 5 locales (en, cs, de, pl, sk)
- Socket token authentication for bridge communication
- Session deduplication by multiplexer slot (sty+window)
- Stable `/list` ordering preserved across restarts via SQLite persistence

## [0.1.0] - 2025-01-20

### Added
- Initial release
- XMPP bridge daemon with GNU Screen and tmux backends
- Unix socket server with JSON protocol
- Session registry with SQLite persistence
- Claude Code hook scripts (8 hooks)
- Fire-and-forget notification (`claude-xmpp-notify`)
- Ask/reply flow (`claude-xmpp-ask`) with bridge and direct XMPP fallback
- systemd user service
- GitHub Actions CI (Python 3.11/3.12/3.13)
