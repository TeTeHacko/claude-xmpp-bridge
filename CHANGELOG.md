# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.25] - 2026-03-10

### Fixed
- **JS plugin:** `session.deleted` handler now resets `registeredSessionID` to
  `null` and clears `reregTimer` when the deleted session matches the currently
  registered one.  Previously the timer kept firing `reportState` for the
  deleted session, which triggered `reregisterIfNeeded` and re-registered the
  session immediately after deletion — an infinite re-register loop.
- **bridge.py `_is_session_alive`:** Added `slot` variable for debug logging
  and improved log message to include the slot identifier (sty:window) when a
  session is detected as dead.

## [0.7.24] - 2026-03-09

### Added
- `tests/test_plugin_sandbox.py` — 13 static invariant tests for the OpenCode
  plugin's sandbox-safe behaviour: `CLIENT_BIN` null fallback (runClient and
  rawRelay return `{exitCode:127}` silently), `setTitle` stdout escape-sequence
  fallback (`\x1bk`), `screenTitleWorks` cache, `.nothrow()` on every bun
  shell `$\`...\`` call, and `registeredSessionID` guards in `pollInbox`,
  `reregTimer` callback, and `reportState`.
- `TestClientSubcommandsWithoutBridge` in `test_client_integration.py` — 3
  integration tests verifying exit codes when bridge is not running: `state`
  exits non-zero with `Error:` in stderr; `register` and `unregister` exit 0
  silently (plugin fire-and-forget contract).

## [0.7.23] - 2026-03-09

### Added
- New integration test file `tests/test_client_integration.py` with 10 tests
  covering the full `claude-xmpp-client` ↔ bridge socket protocol: exit codes
  for `state` (unknown session, known session, bridge not running), agent mode
  updates, and the complete re-registration flow after a simulated bridge
  restart.  Tests use `asyncio.run_in_executor` so the subprocess doesn't
  block the in-process socket server.
- `pytest.mark.integration` marker for subprocess-based tests; run only unit
  tests with `pytest -m "not integration"`.
- Plugin: `REREG_INTERVAL_MS` is now configurable via
  `XMPP_BRIDGE_REREG_INTERVAL_MS` env var (default 90 000 ms) so CI can
  override the interval without waiting 90 seconds.

### Fixed
- `claude-xmpp-client state` now exits non-zero when the bridge is not running
  (socket does not exist).  Previously it silently exited 0, masking the error
  and preventing `reregisterIfNeeded` from triggering re-registration.

### Removed
- Deleted brittle structural tests `TestOpencodePluginBridgeDetection` and
  `TestFindOpencodeDir.test_plugin_contains_source_field` from `test_setup.py`.
  These tests matched specific JS syntax patterns and broke on every refactor
  even when behaviour was correct.  The invariants they checked are now covered
  by the new integration tests.

## [0.7.22] - 2026-03-09

### Fixed
- Plugin: `reportState` now detects bridge errors via stderr content
  (`"Error:"` substring) in addition to exit code, working around cases where
  the bun runtime returns `exitCode: null` for failed subprocesses.
  Added `dbg()` logging of exit code and stderr for every `state` call.
- Plugin: added a periodic re-register timer (`REREG_INTERVAL_MS = 90s`) as a
  reliable fallback for sessions that lose their bridge registration (e.g. after
  a bridge restart). The timer is started after initial registration and
  cancelled on `server.instance.disposed`.
- Plugin: renamed `stateExit` → `stateFailed` (boolean) for clarity.

## [0.7.21] - 2026-03-09

### Fixed
- Plugin now re-registers with the bridge on each `session.idle` event if
  the bridge doesn't recognise the session (exit code ≠ 0 from
  `claude-xmpp-client state`).  This recovers sessions that disappear from
  the bridge DB after a bridge restart — without requiring the user to
  restart OpenCode in every window.

## [0.7.20] - 2026-03-09

### Fixed
- `_is_session_alive` now checks the specific Screen **window** (via
  `screen -S <sty> -p <window> -Q title`) instead of just the screen session
  (`screen -ls`).  Stale sessions in dead windows are now cleaned up on `/list`
  even when the screen session itself is still alive.
- `/list` state icons changed from `⏸`/`▶` to `🟢`/`🔵` to match the circles
  shown in the Screen window title set by the plugin.
- Plugin: `claude-xmpp-client` is now resolved once at startup via `which`.
  If not found (e.g. inside a bwrap sandbox with restricted `$PATH`), all
  bridge calls are silently skipped — no more `bun: command not found` spam
  in the terminal.  `rawRelay` (Bun.spawn) updated likewise.

## [0.7.19] - 2026-03-09

### Changed
- Agent mode indicator replaced by **agent identity indicator**: the left icon
  in the window title and `/list` output now shows a coloured circle matching
  the active OpenCode agent's colour in the TUI, instead of a tool-type icon.
  Default mapping: `build`→🔵, `plan`→🟣, `coder`→🟠, `local`→🩵, unknown→⚪.
  Icons are configurable via env vars `BRIDGE_AGENT_<NAME>` (uppercase).
- Agent is detected from `message.updated` events (field `info.agent`) — the
  only reliable server-side signal, since Tab-switching is client-side only.
- Plugin no longer tracks tool-type mode (`planning`/`code`/`build`); the
  `tool.execute.before` hook now only updates the state circle to 🔵.
- `reportState()` sends the agent emoji directly in the `mode` field instead
  of a string like `"code"`.
- Bridge `_cmd_list()` now uses `agent_mode` value as-is (emoji) instead of
  mapping it through a fixed `mode_icons` dict.
- `opencode.json`: added `"color": "primary"` to `coder` agent and
  `"color": "info"` to `local` agent so their TUI colours match the plugin
  circle icons (🟠 and 🩵 respectively).

## [0.7.18] - 2026-03-08

### Added
- Agent mode indicator ("semafor") for the OpenCode plugin: the window title
  now shows a mode icon to the left of the state circle — `📋` planning
  (default, read-only tools), `✏️` code (edit/write/multiedit), `⚙️` build
  (bash).  Mode icons are configurable via env vars `BRIDGE_MODE_PLANNING`,
  `BRIDGE_MODE_CODE`, `BRIDGE_MODE_BUILD`.
- New `"tool.execute.before"` hook in
  `examples/opencode/plugins/xmpp-bridge.js`: detects the tool being executed
  and updates `currentMode` immediately before each tool call.  Starting a new
  response (`session.status: busy`) resets mode to `"planning"`.
- `agent_mode` column in the sessions DB table (auto-migrated from older
  schemas).  `SessionRegistry.update_state()` now accepts an optional `mode`
  keyword argument that persists the new mode alongside `agent_state`.
- `/list` XMPP output now uses a new format: source icon + mode icon (if
  known) + state icon appear **before** the backend bracket, not inside it.
  Example: `  /1  🧠✏️▶  [screen #2]  v0.7.18  ~/projects/my-app  *`
- Bridge `_handle_state()` extracts an optional `mode` field from the socket
  payload and forwards it to `update_state()`.
- `list_sessions` MCP tool response now includes `agent_mode` per session.

## [0.7.17] - 2026-03-08

### Fixed
- `examples/opencode/plugins/xmpp-bridge.js`: fixed `setTitle()` so the
  Screen window-name escape sequence is written directly to `process.stdout`
  instead of via `$\`printf\`` (which captured stdout into a buffer and never
  reached the terminal).  The function now accepts two arguments —
  `emojiTitle` and `asciiTitle` — and falls back to `process.stdout.write()`
  when `screen -X title` fails (e.g. inside a bwrap sandbox).
- `examples/sandbox/sandbox`: replaced emoji prefix (`🧠`) produced by the
  removed `_detect_icon()` with a short ASCII prefix (`AI. `) from the new
  `_detect_prefix()` function, fixing the corrupted title display in Screen's
  hardstatus bar (Screen counts UTF-8 bytes instead of display columns for
  wide characters).

## [0.7.16] - 2026-03-08

### Fixed
- `client.py` `send_to_bridge`: replaced single `sock.recv(65536)` with a
  loop that reads until `\n`, preventing truncated responses for large
  payloads (e.g. `list_sessions` with many agents).
- `client.py` `_get_socket_token`: added `_check_permissions()` check on
  `socket_token` file — aborts with a clear error if the file has
  group/other-readable permissions (should be `0600`).
- `socket_server.py` `_handle_client`: unexpected exceptions in the request
  handler now send `{"error": "internal error"}` to the client instead of
  silently closing the connection, preventing the client from blocking on
  timeout.

## [0.7.15] - 2026-03-08

### Added
- Email relay for long XMPP notifications: when a notification exceeds
  `email_threshold` characters (default 500), the bridge sends the full
  text via SMTP and truncates the XMPP message to a snippet with a note
  that the full content was sent by email.
- New `email_notify` module with async `send_email()` helper (SMTP,
  no authentication, configurable timeout).
- Three new `Config` fields with layered env/TOML precedence:
  - `smtp_host` (env `CLAUDE_XMPP_SMTP_HOST`, TOML `smtp_host`) — empty
    string disables email relay (default: disabled)
  - `smtp_port` (env `CLAUDE_XMPP_SMTP_PORT`, TOML `smtp_port`) — default 25
  - `email_threshold` (env `CLAUDE_XMPP_EMAIL_THRESHOLD`, TOML
    `email_threshold`) — default 500 characters

## [0.7.14] - 2026-03-08

### Fixed
- OpenCode plugin: `session.status` handler now correctly reads
  `event.properties.status?.type === "busy"` instead of comparing
  `status === "running"` (string); OpenCode sends `status` as an object
  `{ type: "busy" | "idle" }`, so the old comparison never matched and
  the window title never switched to `🧠🔵` while the agent was working

## [0.7.13] - 2026-03-08

### Fixed
- OpenCode plugin: `permission.asked` handler now correctly checks
  `ask-enabled` switch instead of `notify-enabled`

### Changed
- OpenCode plugin: window title uses traffic-light emoji for consistent
  visual width (all fullwidth emoji, no layout shift):
  - `🧠🟢 project` — idle (was `🧠⏸`)
  - `🧠🔵 project` — running (was `🧠▶`)
  - `🧠🔴 project` — requires interaction / permission dialog (was `🧠❓`)
- Plugin header comment updated with traffic-light legend and correct
  switch file names (`notify-enabled` / `ask-enabled`)

## [0.7.12] - 2026-03-08

### Changed
- OpenCode plugin: window title uses two-icon scheme to distinguish states:
  - `🧠⏸ project` — idle
  - `🧠▶ project` — running
  - `🧠❓ project` — requires interaction (permission dialog)
  Previously `🧠❓` was used for both idle and permission states.

## [0.7.11] - 2026-03-08

### Fixed
- `rawRelay()`: add `--` separator before message argument so messages
  starting with `-` are not parsed as CLI flags by `claude-xmpp-client`
- `rawRelay()`: change `stdout:"pipe"` to `stdout:"ignore"` to prevent
  pipe-buffer deadlock if the subprocess emits large stdout output

### Added
- `tests/test_multiplexer.py`: 9 unit tests for `_screen_stuff_escape()`

## [0.7.10] - 2026-03-08

### Fixed
- OpenCode plugin: read `$WINDOW` from `/proc/${process.ppid}/environ`
  instead of `process.env.WINDOW`; the latter can be inherited from a
  wrong context when multiple OpenCode instances share a Screen session
- OpenCode plugin: export `BRIDGE_SESSION_ID`, `BRIDGE_WINDOW`, `WINDOW`
  to `process.env` after registration so agent bash tools can discover
  their own identity without querying the bridge
- OpenCode plugin: replace bun shell template `$\`claude-xmpp-client relay…\``
  with `Bun.spawn()` in `rawRelay()` — bun shell was interpreting `|`,
  `'`, `>` in message content as shell metacharacters, corrupting messages
- Multiplexer: add `_screen_stuff_escape()` — escapes `$` → `\$` and
  `\` → `\\` so GNU Screen's `stuff` command does not expand environment
  variables in message text
- Bridge: `_handle_list()` now includes `sty` field in each session entry
  (was missing; plugin compared `s.sty === STY` but `s.sty` was always `undefined`)
- OpenCode plugin: add `polling` guard flag to prevent concurrent `pollInbox()`
  execution (race between `session.idle` handler and `setInterval` callback)

### Removed
- OpenCode plugin: dead function `shortPath()` (unused since v0.7.0)

## [0.7.9] - 2026-03-08

### Fixed
- OpenCode plugin: resolve window identity by querying bridge at startup
  instead of relying solely on `process.env.WINDOW`

## [0.7.8] - 2026-03-08

### Changed
- Bridge + MCP server: unified inter-agent XMPP notification format —
  robot icon prefix `🤖 sender ──mode──▶ target\n  msg`

## [0.7.7] - 2026-03-08

### Added
- Nudge pattern for inter-agent messaging: `send_message(nudge=true)` enqueues
  the message to the MCP inbox and sends only a bare CR to the terminal,
  avoiding race conditions where a screen inject arrives while the agent
  is busy processing tool calls

## [0.7.6] - 2026-03-08

### Added
- MCP inbox persistence: inbox migrated from in-memory `asyncio.Queue` to
  SQLite `inbox` table in `bridge.db`; messages survive bridge restarts and
  session re-registration

### Fixed
- CLI: added `claude-xmpp-client list` subcommand — outputs all registered
  sessions as JSON to stdout

## [0.7.5] - 2026-03-08

### Fixed
- OpenCode plugin: add 1.5 s delay before `pollInbox()` in `session.idle`
  handler to prevent "assistant message prefill" API error (model was not
  yet fully in awaiting-input state when inject arrived)
- MCP/Bridge: `broadcast_message` and `send_message` with `screen=True` no
  longer double-enqueue into the MCP inbox after a successful screen relay
  (caused messages to be re-delivered on the next `session.idle` poll)

## [0.7.4] - 2026-03-08

### Added
- Registry: `plugin_version` and `agent_state` fields on `SessionInfo` — stored in
  SQLite with automatic schema migration; `plugin_version` is populated from the
  OpenCode plugin registration payload; `agent_state` is updated via the new `state`
  socket command
- Registry: `update_state(session_id, state)` method for updating agent state
- Bridge: `state` socket command — agents report their current state ("idle",
  "running") so the bridge can surface it in `/list` and `list_sessions`
- Bridge: `/list` XMPP output now shows `⏸`/`▶` state icon and `v{version}` for
  each session that has reported state and plugin version
- MCP `list_sessions` and socket `list` command now include `plugin_version` and
  `agent_state` in the response
- CLI: `claude-xmpp-client state '{"session_id":"…","state":"idle"}'` subcommand
- OpenCode plugin: sends `plugin_version` in the `register` payload; updates
  `agent_state` to "idle"/"running" on `session.idle` / model output events

### Fixed
- OpenCode plugin: `isIdle = true` set immediately after `register` so that the
  `setInterval` inbox-polling loop starts running without waiting for the first
  `session.idle` event

## [0.7.3] - 2026-03-08

### Fixed
- MCP: `_handle_relay` no longer enqueues screen-delivered messages into the MCP
  inbox — doing so caused the idle-handler to re-inject already-delivered messages
  on the next `session.idle` event (infinite feedback loop, Bug #1)
- MCP: `send_message(screen=True)` no longer enqueues the message into the inbox —
  every stop/notification sent with `screen=True` was re-delivered to the terminal
  on the next `session.idle` poll (Bug #2)

## [0.7.2] - 2026-03-08

### Fixed
- OpenCode plugin: correct `claude-xmpp-client relay` call syntax (`--to` flag and
  positional message argument); log relay exit code and stderr for debugging

## [0.7.1] - 2026-03-08

### Fixed
- OpenCode plugin: parse SSE (`data: …` lines) response format instead of raw JSON
  for `receive_messages` MCP tool response
- OpenCode plugin: add MCP HTTP initialize step to obtain `mcp-session-id` header
  before calling `tools/call`
- OpenCode plugin: use per-window `session_id` with underscore separator
  (`ses_<sty>_w<window>`) to avoid registry collisions when multiple OpenCode
  instances run inside the same Screen session
- OpenCode plugin: fix `receive_messages` JSON content-block parsing

## [0.7.0] - 2026-03-08

### Added
- MCP `send_message`: `screen` boolean parameter (default `true`) — when `false`,
  the message is enqueued into the MCP inbox only, without terminal relay; useful
  for sessions without a multiplexer or for testing
- MCP tools: structured audit events via `AuditLogger`:
  - `MCP_SEND` — includes `message_id`, `to_session_id`, `screen` flag, `ok`/`reason`
  - `MCP_BROADCAST` — includes `from_session_id`, `delivered`, `failed` counts
  - `MCP_RECEIVE` — includes `session_id`, `count` (emitted only when inbox non-empty)
- MCP `send_message` confirmation now includes `[id:<12-char-uuid>]` for ACK correlation

## [0.6.0] - 2026-03-08

### Added
- MCP server (`BridgeMCPServer`) — exposes bridge functionality as Model Context
  Protocol tools on port 7878 (streamable-HTTP transport); agents communicate
  without screen relay hacks by using standard MCP tool calls:
  - `send_message(to, message, screen=True)` — relay to a specific session
  - `broadcast_message(message, sender_session_id)` — relay to all other sessions
  - `receive_messages(session_id)` — drain MCP inbox queue for a session
  - `list_sessions()` — enumerate all registered sessions with metadata
- Config: `mcp_port` (default 7878, set to 0 to disable)
- CLI: `--mcp-port` flag and `CLAUDE_XMPP_MCP_PORT` environment variable

## [0.5.0] - 2026-03-08

### Added
- Socket `list` command — agents can discover all registered sessions with full
  metadata (`session_id`, `project`, `backend`, `window`, `source`, `index`)
- Relay `to_project` targeting — `relay` can target a session by project path prefix
  (with `~` expansion), without knowing the session ID in advance
- Heartbeat: background task runs `_cleanup_stale_sessions` every 60 s, removing
  dead Screen/tmux windows automatically from the registry
- OpenCode plugin: use `$WINDOW` env var directly instead of `screen -Q info`

## [0.4.0] - 2026-03-08

### Added
- Socket `relay` command — send a message to a specific session by `session_id`,
  `index`, or `to_project`; all inter-agent traffic is forwarded to the XMPP
  observer so the human can monitor agent conversations
- Socket `broadcast` command — send a message to all sessions except the sender
- CLI: `claude-xmpp-client relay --to SESSION_ID MESSAGE`
- CLI: `claude-xmpp-client broadcast --session-id SENDER MESSAGE`
- Audit events: `RELAY_SENT`, `RELAY_FAILED`, `BROADCAST_SENT`
- XMPP startup notification now includes bridge version

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
