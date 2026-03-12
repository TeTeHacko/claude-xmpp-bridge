# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.3] - 2026-03-11

### Added
- **Release guard for plugin-only changes** — test coverage now fails if the
  OpenCode plugin changes relative to `HEAD` without also changing the package
  version, preventing future `pipx upgrade` misses when only plugin assets were
  updated.

## [0.8.2] - 2026-03-11

### Fixed
- **OpenCode plugin shutdown no longer waits on bridge/helper cleanup** — end
  notification, bridge unregister, and Screen dynamictitle restore are now sent
  as best-effort background cleanup instead of blocking `server.instance.disposed`,
  reducing slow plugin shutdowns.

## [0.8.1] - 2026-03-11

### Fixed
- **OpenCode plugin shutdown no longer forces a title reset escape sequence** —
  the dispose path now clears timers and restores Screen dynamictitle without
  explicitly writing a title reset during shutdown, avoiding terminal garbage in
  some Screen/OpenCode exit flows.

## [0.8.0] - 2026-03-11

### Changed
- **Setup wizard now auto-configures OpenCode title-only mode** — when the user
  installs the OpenCode plugin without selecting `bridge-daemon`, the wizard now
  writes the plugin with `title-only` as the default bridge mode instead of
  requiring a manual environment override later.
- **OpenCode plugin now reports diagnostics through structured plugin logs** —
  bridge/plugin warnings and errors go through `client.app.log(...)` with
  `info`/`warn`/`error` levels and throttling instead of contributing raw noise
  to the terminal UI.

## [0.7.49] - 2026-03-11

### Added
- **Title-only OpenCode plugin mode** — set `XMPP_BRIDGE_MODE=title-only` to keep
  Screen/tmux title indicators while disabling all bridge/MCP traffic.
- **Auto-disable when bridge is missing** — set
  `XMPP_BRIDGE_DISABLE_WHEN_MISSING=1` to let the plugin switch permanently into
  title-only mode when startup bridge calls fail.

## [0.7.48] - 2026-03-11

### Changed
- **OpenCode plugin now degrades quietly without a bridge** — when the local
  bridge/MCP server is unavailable, the plugin stops fast polling/re-register
  loops, suppresses repeated failed bridge calls during idle events, skips helper
  scripts until registration succeeds, and retries recovery in the background on
  a slower timer.

## [0.7.47] - 2026-03-11

### Changed
- **Session overview now exposes liveness and reply telemetry** — `list`,
  `list_sessions`, and `get_session_context` now include `last_seen`,
  derived `idle_seconds`, and `last_agent_sender`, making it easier to spot
  idle/stuck agents and see the current reply target without an extra lookup.

## [0.7.46] - 2026-03-11

### Added
- **Socket/CLI parity for reply-to-sender flow** — local bridge clients can now
  use `reply_to_last_sender` via the socket API and `claude-xmpp-client reply-last`
  instead of relying on MCP-only access for direct agent replies.

## [0.7.44] - 2026-03-11

### Changed
- **MCP relay metadata can now identify the sender** — `send_message` accepts
  optional `sender_session_id`, and generated relay payloads now propagate it in
  the JSON `from` field so receiving agents can reply directly to the originating
  session instead of responding to the human observer.

## [0.7.45] - 2026-03-11

### Added
- **Reply to the last agent sender via MCP** — the bridge now remembers the last
  non-null relay sender seen by `receive_messages(session_id)` and exposes
  `reply_to_last_sender(session_id, message, nudge=True)` so agents can answer
  other agents directly without manually parsing relay metadata.

### Changed
- **Session context now includes `last_agent_sender`** — MCP context payloads can
  expose the currently remembered reply target for the session.

### Documentation
- Documented the recommended reply flow using `BRIDGE_SESSION_ID` as
  `sender_session_id` when agents call MCP `send_message`.

## [0.7.43] - 2026-03-10

### Added
- **Bridge-native MCP todo/context API** — MCP now exposes `get_session_context`,
  `list_todos`, `replace_todos`, `add_todo`, `update_todo`, and `remove_todo`.
  Session context includes todos, bridge-native file locks, `todo_count`,
  `lock_count`, `inbox_count`, and `todos_version` so agents can coordinate
  through MCP without shell-side helpers.

### Changed
- **OpenCode plugin reports a build-aware ref** — plugin registration now sends
  `plugin_version` as `semver+hash`, where the hash is derived from the plugin's
  own source file. This means local plugin-only changes remain visible in `/list`
  even when the Python package version did not change.
- **`/list` shows compact plugin build refs** — when a plugin reports
  `0.7.43+abc1234`, the human-facing session list displays `@abc1234` instead of
  the full semantic version string.
- **Bridge shutdown notification includes version** — XMPP stop messages now show
  the bridge version that was running, matching the startup notification.
- **Todo writes now use optimistic locking** — `replace_todos` supports
  `expected_version`, and todo mutations increment `todos_version` to prevent
  silent last-write-wins overwrites.

### Fixed
- **Bridge-native file locks are safer** — lock acquisition is now atomic,
  cleanup of stale locks respects project filtering, and filepaths are normalized
  before lock identity checks so equivalent paths cannot create duplicate locks.

### Tests
- Added coverage for MCP todo/context APIs, optimistic todo version conflicts,
  atomic todo item operations, build-aware plugin refs, and lock regression cases
  (atomic acquire, project-filtered cleanup, normalized paths).

## [0.7.42] - 2026-03-10

### Added
- **Bridge-native MCP file locks** — the bridge now stores file locks in SQLite
  and exposes them via MCP: `acquire_file_lock`, `release_file_lock`,
  `list_file_locks`, and `cleanup_stale_locks`. This is the first major step
  toward MCP-only multi-agent coordination without shell-side lock helpers.

### Changed
- **`list_file_locks` / `cleanup_stale_locks` merge native and legacy locks** —
  MCP now returns both bridge-native locks (`source: "bridge"`) and legacy
  lock-hint files from `~/.claude/working` (`source: "legacy"`), so agents can
  migrate gradually while still seeing the full coordination picture.
- **Inter-agent terminal injections are explicitly marked as generated** —
  relay/broadcast deliveries are wrapped in a `[bridge-generated message]` block
  with a JSON metadata line, making it obvious in shared Screen/tmux windows
  that the text came from the bridge and not a human.
- **OpenCode plugin reduces no-bridge noise** — when the bridge/MCP server is
  down, the plugin enters a retry cooldown (`XMPP_BRIDGE_RETRY_MS`) and stops
  hammering bridge calls on every idle/state event, which reduces expected error
  spam in logs/SIEM.

### Fixed
- **Screen integration stabilised further** — title updates are debounced,
  startup title work is deferred, `tool.execute.before` no longer triggers
  Screen redraws, and per-window `hstatus` cleanup avoids prompt garbage in
  hardstatus/caption without corrupting the terminal.
- **Generated relay messages no longer double-wrap** — nudge/inbox delivery
  paths detect an already wrapped bridge-generated payload and leave it intact.

### Tests
- Added and extended coverage for generated-message formatting, Screen title
  behavior, no-bridge plugin cooldown, native file lock registry, and MCP file
  lock tools.

## [0.7.41] - 2026-03-10

### Fixed
- **Screen/OpenCode redraw artefacts — stop updating Screen title from `tool.execute.before`**.
  The previous implementation still called `screen -X title` from very frequent
  runtime events while OpenCode TUI was actively rendering, which could collide
  with GNU Screen caption/hardstatus redraws in some setups and produce repeated
  status lines, flicker, and window-list garbage.

  The plugin now:
  - removes title updates from `tool.execute.before` entirely (it still reports
    `running` state to the bridge)
  - debounces all title writes via a scheduler (`XMPP_BRIDGE_TITLE_DEBOUNCE_MS`,
    default 750 ms)
  - defers startup title setup via `setImmediate()` so it does not run during
    the most fragile part of OpenCode TUI initialisation
  - keeps title changes only on coarse state transitions (`startup`,
    `session.created`, `session.status`, `session.idle`, `permission.*`)
  - clears pending title timers during shutdown cleanup

### Added
- **Plugin redraw-safety tests** — structural tests now verify that title updates
  are scheduled/debounced, that `tool.execute.before` no longer changes the
  title, and that shutdown clears pending title timers.
- **Screen title integration smoke test** — new integration coverage exercises
  real `screen -X title`, `screen -X dynamictitle`, and `screen -Q title`
  against an isolated Screen session with aggressive `caption`/`hardstatus` /
  `backtick` settings.

## [0.7.40] - 2026-03-10

### Fixed
- **Root cause of Screen TUI artefacts identified and fixed** — OpenCode TUI
  sends `\033]0;OpenCode\007` (OSC 0 window title escape) on every redraw.
  Screen intercepts this from the pty and updates the window title, which
  triggers a caption/hardstatus redraw.  Combined with `backtick` intervals
  in `.screenrc` this caused constant caption redraws colliding with OpenCode's
  own rendering → doubled window lists, flickering, garbage characters.

  **Fix:** plugin now runs `screen -X dynamictitle off` at startup (outside
  sandbox).  This tells Screen to ignore title escape sequences from the pty
  of that window — OpenCode's OSC 0 writes are silently discarded.  The plugin
  retains full control of the window title via `screen -X title` (socket IPC,
  no pty involvement).  `dynamictitle on` is restored on `server.instance.disposed`.

## [0.7.39] - 2026-03-10

### Fixed
- **Screen hardstatus artefacts (race condition with backtick redraws)** —
  `setTitle()` was calling `screen -X title` on every state change event
  (`tool.execute.before`, `session.status`, `session.idle`, `message.updated`).
  With `backtick 2 1 1 uptime` in `.screenrc` (1-second hardstatus redraw),
  concurrent `screen -X title` calls collided with Screen's internal redraw,
  producing doubled window lists, flickering, and garbage in the statusbar.

  **Fix:** added a `lastTitle` cache — `setTitle()` now skips `screen -X title`
  (and stdout/OSC writes) when the title string has not changed since the last
  call.  The title only changes on actual state transitions (idle→running,
  running→idle, agent switch, permission dialog), so the number of `screen -X`
  calls drops from O(tool calls) to O(state changes).

## [0.7.38] - 2026-03-10

### Fixed
- **`detectSandbox()` no longer calls `screen -Q`** — the previous implementation
  ran `screen -S $STY -Q title` to probe socket availability, but `screen -Q`
  writes "session not found" errors to stderr which leaked into the OpenCode TUI
  as visible error lines.  Detection is now done by checking whether the Screen
  socket file exists on the filesystem (`fs.existsSync($SCREENDIR/$STY)`), which
  is silent, synchronous, and has no side effects.  `detectSandbox` is also now
  a plain (non-async) function since no subprocess is needed.

## [0.7.37] - 2026-03-10

### Fixed
- **Screen TUI artefacts on startup** — the plugin's `setTitle()` was falling back
  to writing raw `ESC k ... ESC \` escape sequences directly to stdout whenever
  `screen -X title` failed (including transient failures at startup due to a race
  condition before the Screen socket is ready).  Writing escape sequences to the
  inherited pty while OpenCode TUI is active causes Screen to redraw `caption` /
  `hardstatus` at the wrong moment, producing doubled window lists, flickering,
  and garbage characters (especially visible with `altscreen on` + `caption always`
  in `.screenrc`).

  **Fix:** sandbox is now detected **once at startup** via `screen -S $STY -Q title`.
  The stdout escape fallback is used **only** when `inSandbox === true` (bwrap
  `--new-session` → no socket access).  Outside sandbox, `screen -X title` is
  always used; transient failures are silently ignored (next `setTitle()` call
  will succeed).

- **`session-start-title.sh` hook** — same issue: the hook was unconditionally
  writing `\033k...\033\\` to stderr (the inherited Screen pty).  Now tries
  `screen -S $STY -p $WINDOW -X title` first; falls back to escape sequences
  only when the socket is unavailable (sandbox / no Screen).

## [0.7.36] - 2026-03-10

### Fixed
- **`send()` uses `self.is_connected` internally** — the method was still calling
  `self.connected.is_set()` directly instead of the new `is_connected` property,
  creating an inconsistency in the class's own API.
- **`TestBackoffEscalation` now tests real logic** — the test previously manually
  incremented `_backoff` (duplicating constants, not testing code).  It now calls
  `_on_disconnected()` directly so any change to the backoff formula is caught.

### Changed
- **`_on_disconnected` docstring extended** — documents the `_should_reconnect=False`
  branch that skips reconnect scheduling.

## [0.7.35] - 2026-03-10

### Added
- **`XMPPConnection.is_connected` property** — convenience property that mirrors
  `conn.connected.is_set()` so callers no longer need to access the internal
  asyncio `Event` directly.
- **Additional `test_xmpp.py` coverage** — new test classes cover `is_connected`,
  `_on_message` callback dispatch (including no-callback and replacement cases),
  `send()` HTML stripping (`del msg["html"]`), and `start()` event-handler
  registration / force-STARTTLS path.

### Changed
- **`XMPPConnection` private methods gain docstrings** — `_on_session_start`,
  `_on_message`, and `_on_disconnected` now have concise docstrings describing
  their purpose and side-effects.

## [0.7.34] - 2026-03-10

### Changed
- **`email_threshold` default raised from 500 to 4000** — the previous 500-char
  threshold was too aggressive, causing routine messages (e.g. 532 chars) to be
  truncated on XMPP with "full message sent by email" even though XMPP handles
  multi-KB messages fine.  The new 4000-char default keeps XMPP messages intact
  for typical usage and only triggers email relay for genuinely large payloads
  (full file contents, long diffs, etc.).  Configurable via `email_threshold`
  in config.toml or `CLAUDE_XMPP_EMAIL_THRESHOLD` env var.

## [0.7.33] - 2026-03-10

### Added
- **`XMPP_OUT` audit event** — every outgoing XMPP message is now recorded
  in the structured audit log with `recipient`, `body_len`, `original_len`,
  `email_relay` (bool), and `ok` fields.  Previously there was no visibility
  into what the bridge actually sent.
- **INFO-level log when email relay triggers** — `_xmpp_send` now logs the
  message length, threshold, and SMTP target so email relay activation is
  visible in production logs (`journalctl`).

### Changed
- **`email_notify.py` success log promoted from DEBUG to INFO** — previously
  `"Email sent to …"` was only visible at DEBUG level, making it impossible
  to confirm delivery in production.

### Fixed
- **Stale test mocks** — three email-relay tests (`test_short_message_no_email`,
  `test_no_email_when_smtp_host_empty`, `test_exactly_threshold_length_no_email`)
  were mocking `asyncio.ensure_future` but the code has used `asyncio.create_task`
  since v0.7.31.  The assertions were silently passing without testing anything.
  Now correctly mock `asyncio.create_task`.

## [0.7.32] - 2026-03-10

### Changed
- **Refactor: `multiplexer.py` extracted `_run_cmd` helper** — eliminated 6×
  duplicated subprocess + timeout + error-handling boilerplate.  Added named
  constants `_CMD_TIMEOUT` (5 s) and `_INTER_CMD_DELAY` (50 ms).
- **Refactor: `socket_server.py` switched from `reader.read()` to
  `reader.readline()`** — line-based protocol now correctly reads exactly one
  JSON request per connection instead of waiting for EOF.  Added explicit size
  guard (64 KB max).
- **Refactor: `bridge.py` `_ask_queue` changed from `list` to
  `collections.deque`** — `popleft()` O(1) for the common case; `remove()`
  fallback only for mid-queue timeouts.
- **Refactor: `messages.py` removed `object.__setattr__` hack** — frozen
  dataclass overrides now use constructor kwargs via `Messages(**overrides)`.
- **Refactor: `bridge.py` extracted `_sorted_ids` helper** — deduplicated
  session sort logic from 3 call sites.
- **`__init__.py` added `__all__`** for explicit public API.
- **`audit.py` creates parent directory** for file-based audit log target if
  it does not exist (prevents `FileNotFoundError`).
- **`config.py` `_toml_str` logs warning** on non-string TOML values instead
  of silently coercing them.

## [0.7.31] - 2026-03-10

### Fixed
- **CRITICAL: `_TARGET_RE` accepted empty target strings** — regex quantifier
  changed from `{0,128}` to `{1,128}` in `multiplexer.py`.
- **CRITICAL: `_check_permissions` silently passed on `OSError`** — now logs a
  warning instead of silently ignoring permission-check failures.
- **CRITICAL: `force_starttls = bool(val)` evaluated `"no"` as `True`** — added
  proper string-to-bool parsing in `config.py`.
- **`asyncio.ensure_future` fire-and-forget lost errors** — replaced with
  `asyncio.create_task` + `_email_task_done` callback in `bridge.py`.
- **Client recv loop had no size limit** — added 1 MB cap in `client.py`.
- **tmux liveness check missing `OSError` handling** — added in `bridge.py`.
- **`config.py` `int()` env var parsing lacked error handling** — added
  `ValueError` → `SystemExit` with `from None` for `SMTP_PORT` and
  `EMAIL_THRESHOLD`.
- **Registry error message said "colons" but regex excluded them** — fixed
  error text in `registry.py`.
- **`email_notify.py` had broad `except Exception`** — narrowed to
  `SMTPException`, `OSError`, `TimeoutError`.
- **`socket_server.py` had broad `except Exception`** — split
  `UnicodeDecodeError` + `log.exception` for unexpected errors.
- **Redundant `import json as _json` in `cli.py`** — removed.
- **`_source_icon` created merged dict on every call** — icons dict now cached
  in `XMPPBridge.__init__`.
- **tmux pane IDs (`%3`) rejected by `STY_RE`** — added `%` to allowed
  characters.

### Changed
- `_short_path` in `mcp_server.py` now delegates to `bridge._short_path` when
  bridge is available, eliminating code duplication.

## [0.7.30] - 2026-03-10

### Changed
- **JSON inter-agent XMPP notifications:** Relay and broadcast XMPP observer
  messages now use structured JSON instead of plain-text emoji format.  This
  makes inter-agent traffic machine-parseable for LLMs and auditable for humans.
  Format: `{"type": "relay"|"broadcast", "mode": "nudge"|"screen"|"inbox",
  "from": "...", "to": "...", "message": "...", "ts": 1741612800.123}`.
  MCP server messages also include a `"message_id"` field.
- Message bodies are no longer truncated in XMPP notifications (full text is
  included in the JSON payload).

## [0.7.29] - 2026-03-10

### Fixed
- **`bridge.py` `_screen_query_locks` memory leak:** Per-STY `asyncio.Lock`
  instances used to serialize `screen -Q` queries were never cleaned up when
  sessions were removed.  `_cleanup_stale_sessions` now prunes locks for STY
  values that no longer have any registered session.

### Removed
- Dead code `_ALIVE_CHECK_CMDS` dict (unused since v0.7.26 when `_is_session_alive`
  was rewritten to use socket-file checks and `_screen_window_alive`).

## [0.7.28] - 2026-03-10

### Fixed
- **`bridge.py` `_screen_window_alive`:** Serialize `screen -S <sty> -Q title`
  calls per STY using `asyncio.Lock`.  GNU Screen's `-Q` flag creates a
  temporary `-queryA` socket; concurrent `-Q` calls on the same session collide
  and return exit 1 with "There is already a screen running", causing the bridge
  to incorrectly mark live sessions as dead.  This was the root cause of alloy
  (and other) sessions being repeatedly cleaned up and re-registered every
  cleanup cycle.

## [0.7.27] - 2026-03-10

### Added
- **`registry.py`:** `SessionInfo` now includes a `last_seen` field (Unix timestamp).
  The field is persisted in a new `last_seen REAL` column (auto-migrated on startup)
  and updated on every `state` command from the plugin (heartbeat).
- **`bridge.py` `_screen_window_alive(sty, window)`:** New async helper that runs
  `screen -S <sty> -p <window> -Q title` to check whether a specific screen window
  still exists.  Returns `True` on timeout/OSError to avoid false-positive dead
  detection.
- **`bridge.py` `_is_session_alive` (screen backend):** Now performs a three-stage
  liveness check: (1) socket-file existence (fast, no subprocess), (2) window-level
  subprocess check via `_screen_window_alive`, (3) heartbeat TTL check — if
  `last_seen` is set and older than `HEARTBEAT_TTL` (300 s ≈ 5 min, ~3 missed
  heartbeats), the session is considered dead.  This eliminates stale DB sessions
  that survive bridge restarts when opencode has already exited but the screen
  window/socket remain.

### Fixed
- Stale sessions (opencode exited, bash still running in screen window) no longer
  appear in `/list` after a bridge restart.  Previously they would persist
  indefinitely because the socket and window checks both returned alive.

## [0.7.26] - 2026-03-10

### Fixed
- **`_is_session_alive` (screen backend):** Replaced `screen -Q title` subprocess
  with a direct socket-file existence check (`~/.screen/<sty>`).  The subprocess
  approach silently failed from a TTY-less process (systemd user service): screen
  reported attached sessions as dead, causing the bridge to delete live sessions
  every 60 s and spam `Error: session not found` in Claude windows.  The socket
  file is accessible without a controlling terminal from any process of the same
  user.  New helper methods `_screen_socket_path(sty)` and `_screen_socket_alive(sty)`
  implement the check; all affected tests were updated to mock `_screen_socket_alive`
  instead of `asyncio.create_subprocess_exec`.

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
