# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
