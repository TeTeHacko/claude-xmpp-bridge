"""Sandbox-safety and redraw-safety invariants for the OpenCode plugin.

These tests verify that the plugin is safe to run in a restricted environment
(bwrap sandbox, corporate network without bridge) by checking structural
invariants in the plugin source code.

Why static analysis instead of running the plugin:
  The project has no JavaScript test infrastructure.  The plugin runs inside
  the OpenCode process (embedded Bun runtime) — there is no easy way to unit-
  test it in isolation.  Static checks on the source are the next best thing:
  they catch regressions immediately and are fast (< 1 ms each).

Invariants checked:
   1. CLIENT_BIN fallback — runClient returns immediately without
      any subprocess or console output when CLIENT_BIN is null.
  2. scheduleTitle/applyTitleNow — sandbox fallback uses process.stdout.write,
     while non-sandbox updates go through screen -X title with debounce.
  3. Startup title setup is deferred (setImmediate), avoiding redraws during
     OpenCode TUI initialisation.
  4. tool.execute.before must NOT update the title — only report running state.
  5. .nothrow() on every bun shell $`...` call — no unhandled exceptions from
     missing external tools (agent-notify.sh, test -f, screen -X).
  6. registeredSessionID guards — pollInbox, reregTimer callback, and
     reportState all return immediately when no session is registered, so no
     HTTP or socket calls are made in sandbox mode.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Helper: load plugin source once
# ---------------------------------------------------------------------------


def _plugin_path() -> Path:
    """Return the path to xmpp-bridge.js in the source tree."""
    here = Path(__file__).parent
    candidates = [
        here.parent / "examples" / "opencode" / "plugins" / "xmpp-bridge.js",
        here.parent / "share" / "claude-xmpp-bridge" / "opencode" / "plugins" / "xmpp-bridge.js",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError("xmpp-bridge.js not found in source tree")


def _plugin_text() -> str:
    return _plugin_path().read_text()


def _function_body(text: str, fn_start_pattern: str) -> str:
    """Extract the body of a JS function/arrow starting at fn_start_pattern.

    Finds the opening brace after fn_start_pattern and returns everything up
    to the matching closing brace (handles nesting).  Returns empty string if
    not found.
    """
    idx = text.find(fn_start_pattern)
    if idx == -1:
        return ""
    # Find the first '{' after the pattern
    brace_start = text.find("{", idx)
    if brace_start == -1:
        return ""
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : i + 1]
    return ""


# ---------------------------------------------------------------------------
# TestPluginClientBinFallback
# ---------------------------------------------------------------------------


class TestPluginClientBinFallback:
    """runClient must be a no-op when CLIENT_BIN is null; injectViaPromptAsync uses HTTP."""

    def test_runClient_returns_127_silently_when_no_client_bin(self):
        """runClient must return {exitCode:127} immediately without any subprocess
        or console output when CLIENT_BIN is falsy.
        """
        text = _plugin_text()
        body = _function_body(text, "const runClient = async")
        assert body, "runClient function not found in plugin"

        # First branch must be the CLIENT_BIN guard
        assert "!CLIENT_BIN" in body, "runClient must check !CLIENT_BIN as first guard"
        assert "exitCode: 127" in body, "runClient must return exitCode:127 when CLIENT_BIN is null"
        # Must not print anything to console
        assert "console.log" not in body, "runClient must not call console.log"
        assert "console.error" not in body, "runClient must not call console.error"
        assert "console.warn" not in body, "runClient must not call console.warn"

    def test_runClient_uses_bun_spawn_with_piped_output(self):
        text = _plugin_text()
        body = _function_body(text, "const runClient = async")
        assert body, "runClient function not found in plugin"
        assert "Bun.spawn" in body, "runClient must use Bun.spawn for quiet subprocess handling"
        assert 'stdout: "pipe", stderr: "pipe"' in body, (
            "runClient must capture stdout/stderr instead of leaking client output to the terminal"
        )
        assert "new Response(proc.stderr).text()" in body, "runClient must read stderr explicitly"
        assert "$`${CLIENT_BIN} ${args}`" not in body, "runClient must not use Bun shell interpolation anymore"

    def test_runAgentNotify_uses_quiet_spawn_helper(self):
        text = _plugin_text()
        assert "const runQuietCommand = async (argv) => {" in text, "plugin must define quiet subprocess helper"
        body = _function_body(text, "const runAgentNotify = async")
        assert body, "runAgentNotify helper not found in plugin"
        assert "runQuietCommand([AGENT_NOTIFY_BIN, ...args])" in body
        assert "agent-notify exit=" in body, "agent-notify errors should go to structured logs"

    def test_agent_notify_no_longer_uses_bun_shell_templates(self):
        text = _plugin_text()
        assert "agent-notify.sh" in text, "plugin should still reference agent-notify helper"
        assert "$`${process.env.HOME}/claude-home/agent-notify.sh" not in text, (
            "agent-notify must not be executed through Bun shell templates anymore"
        )

    def test_injectViaPromptAsync_exists_and_uses_http(self):
        """injectViaPromptAsync must use HTTP prompt_async endpoint for push delivery."""
        text = _plugin_text()
        body = _function_body(text, "const injectViaPromptAsync = async")
        assert body, "injectViaPromptAsync function not found in plugin"

        assert "prompt_async" in body, "must use prompt_async endpoint"
        assert "fetch(" in body, "must use fetch for HTTP call"
        assert "opencodeSessionID" in body, "must use raw OpenCode session ID"

    def test_runClient_guard_is_first_statement(self):
        """The !CLIENT_BIN guard must be the very first statement in runClient,
        not buried after other logic that might have side effects.
        """
        text = _plugin_text()
        body = _function_body(text, "const runClient = async")
        assert body, "runClient function not found in plugin"

        # Strip the outer braces and leading whitespace
        inner = body.strip().lstrip("{").strip()
        # First non-comment, non-whitespace content must be the guard
        # Remove single-line comments
        inner_no_comments = re.sub(r"//[^\n]*", "", inner).strip()
        assert inner_no_comments.startswith("if (!CLIENT_BIN)"), (
            f"runClient first statement must be 'if (!CLIENT_BIN)', got: {inner_no_comments[:80]!r}"
        )

    def test_injectViaPromptAsync_checks_opencodeSessionID(self):
        """injectViaPromptAsync must check opencodeSessionID before making HTTP call."""
        text = _plugin_text()
        body = _function_body(text, "const injectViaPromptAsync = async")
        assert body, "injectViaPromptAsync function not found in plugin"

        inner = body.strip().lstrip("{").strip()
        inner_no_comments = re.sub(r"//[^\n]*", "", inner).strip()
        assert "!text" in inner_no_comments[:100] or "!ocID" in inner_no_comments[:200], (
            "injectViaPromptAsync must guard against empty text or missing session ID early"
        )

    def test_runBridgeClient_suppresses_calls_when_bridge_unavailable(self):
        """When bridge is down, plugin must enter a cooldown instead of hammering
        the client binary / MCP endpoint and spamming SIEM with expected errors.
        """
        text = _plugin_text()
        assert "BRIDGE_RETRY_MS" in text, "bridge retry cooldown constant must exist"
        assert "bridgeUnavailableUntil" in text, "bridge availability cooldown state must exist"
        assert "const runBridgeClient = async" in text, "runBridgeClient helper must exist"
        assert "bridgeSuppressed()" in text, "runBridgeClient must consult bridgeSuppressed()"
        assert "markBridgeUnavailable" in text, "plugin must mark bridge unavailable on expected bridge errors"
        assert "BRIDGE_RECOVERY_POLL_MS" in text, "plugin must define a slow recovery poll interval"
        assert "ensureRecoveryTimer" in text, "plugin must define a degraded-mode recovery timer"
        assert "bridgeDisabled" in text, "plugin must support full bridge-disabled mode"
        assert "client.app.log" in text, "plugin should use structured OpenCode logging instead of raw terminal output"

    def test_agent_notify_has_own_availability_check(self):
        text = _plugin_text()
        assert "const AGENT_NOTIFY_AVAILABLE = helperExists(AGENT_NOTIFY_BIN)" in text, (
            "agent-notify helper should have explicit availability detection"
        )
        body = _function_body(text, "const runAgentNotify = async")
        assert body, "runAgentNotify helper not found in plugin"
        assert "if (!AGENT_NOTIFY_AVAILABLE) return { exitCode: 127" in body, (
            "runAgentNotify must no-op when helper script is absent"
        )

    def test_plugin_uses_structured_log_levels_and_throttling(self):
        text = _plugin_text()
        assert 'const logPlugin = (level, msg, key = "") =>' in text, "plugin must centralize logging"
        assert 'const warn = (msg, key = "") => logPlugin("warn", msg, key)' in text
        assert 'const errlog = (msg, key = "") => logPlugin("error", msg, key)' in text
        assert 'const logCaught = (scope, err, key = "") => errlog(' in text
        assert "const lastLogAt = new Map()" in text, "plugin logs should be throttled by key"
        assert "XMPP_BRIDGE_LOG_THROTTLE_MS" in text, "plugin must expose log throttle env var"

    def test_startup_async_blocks_log_caught_errors(self):
        text = _plugin_text()
        assert 'await logCaught("startup-title", err, "startup-title-error")' in text
        assert 'await logCaught("startup-register", err, "startup-register-error")' in text

    def test_tool_execute_before_logs_caught_errors(self):
        text = _plugin_text()
        body = _function_body(text, '"tool.execute.before": async')
        assert body, "tool.execute.before handler not found in plugin"
        assert 'await logCaught("tool.execute.before", err, "tool-execute-before-error")' in body

    def test_event_handler_logs_caught_errors(self):
        text = _plugin_text()
        idx = text.find("event: async ({ event }) =>")
        assert idx != -1, "event handler not found in plugin"
        body = text[idx : idx + 12000]
        assert "try {" in body, "event handler must wrap event processing in try/catch"
        assert "await logCaught(`event:${event.type}`, err, `event-error:${event.type}`)" in body

    def test_runBridgeClient_short_circuits_when_bridge_disabled(self):
        text = _plugin_text()
        body = _function_body(text, "const runBridgeClient = async")
        assert body, "runBridgeClient function not found in plugin"
        assert "if (bridgeDisabled) return { exitCode: 126" in body, (
            "runBridgeClient must short-circuit immediately in title-only mode"
        )

    def test_pollInbox_skips_when_bridge_suppressed(self):
        text = _plugin_text()
        body = _function_body(text, "const pollInbox = async")
        assert body, "pollInbox function not found in plugin"
        assert "bridgeSuppressed()" in body, "pollInbox must skip MCP polling during bridge cooldown"
        assert "bridgeDisabled" in body, "pollInbox must skip all work in title-only mode"

    def test_reportState_skips_when_bridge_suppressed(self):
        text = _plugin_text()
        body = _function_body(text, "const reportState = async")
        assert body, "reportState function not found in plugin"
        assert "bridgeSuppressed()" in body, "reportState must not call bridge while cooldown is active"
        assert "if (bridgeDisabled) return true" in body, "reportState must short-circuit in title-only mode"

    def test_failed_registration_enters_recovery_mode(self):
        text = _plugin_text()
        assert "stopActiveBridgeTimers()" in text, "plugin must stop active bridge timers when registration fails"
        assert "ensureRecoveryTimer()" in text, "plugin must start recovery timer when registration fails"

    def test_agent_notify_helpers_run_only_with_registered_session(self):
        text = _plugin_text()
        assert "if (registeredSessionID && AGENT_NOTIFY_AVAILABLE)" in text, (
            "startup/session-created notify helper must only run when bridge registration succeeded"
        )

    def test_title_only_mode_can_disable_bridge_when_missing(self):
        text = _plugin_text()
        assert "XMPP_BRIDGE_MODE" in text, "plugin must support explicit bridge mode override"
        assert "XMPP_BRIDGE_DISABLE_WHEN_MISSING" in text, (
            "plugin must support auto-disabling bridge logic when bridge is missing at startup"
        )

    def test_plugin_registers_build_aware_plugin_ref(self):
        """The registration payload should use a build-aware plugin ref, not only
        the static PLUGIN_VERSION constant, so plugin-only local changes are visible
        in /list even when the Python package version did not change.
        """
        text = _plugin_text()
        assert "const pluginRef = (() => {" in text, "pluginRef self-hash helper must exist"
        body = _function_body(text, "const makeRegPayload =")
        assert body, "makeRegPayload function not found in plugin"
        assert "plugin_version: pluginRef" in body, (
            "registration payload must send pluginRef instead of plain PLUGIN_VERSION"
        )


# ---------------------------------------------------------------------------
# TestPluginTitleFallback
# ---------------------------------------------------------------------------


class TestPluginTitleFallback:
    """Title updates must be scheduled and sandbox-safe."""

    def test_applyTitleNow_has_stdout_escape_sequence_fallback(self):
        """applyTitleNow must write ESC k ... ESC \\ to stdout as fallback for bwrap
        sandboxes where screen socket is inaccessible.
        """
        text = _plugin_text()
        body = _function_body(text, "const applyTitleNow = async")
        assert body, "applyTitleNow function not found in plugin"

        assert "process.stdout.write" in body, "applyTitleNow must write to process.stdout as sandbox fallback"
        # Screen title escape: ESC k ... ESC backslash
        assert r"\x1bk" in body, r"applyTitleNow stdout fallback must use \x1bk (Screen title escape sequence)"
        assert r"\x1b\\" in body, r"applyTitleNow stdout fallback must close with \x1b\\ (Screen title terminator)"

    def test_applyTitleNow_stdout_path_reachable_only_in_sandbox(self):
        """The stdout.write path must be guarded by inSandbox — it must only run
        when the bwrap sandbox is detected, never outside it.

        Logic must be:
          if (STY && !inSandbox) { screen -X ... }
          if (STY && inSandbox)  { stdout.write ... }
        """
        text = _plugin_text()
        body = _function_body(text, "const applyTitleNow = async")
        assert body, "applyTitleNow function not found in plugin"

        lines = body.splitlines()
        stdout_line_idx = next(
            (i for i, ln in enumerate(lines) if r"\x1bk" in ln),
            None,
        )
        assert stdout_line_idx is not None, r"No \x1bk line found in setTitle"

        # The stdout.write line must be preceded by `if (STY && inSandbox)` within a few lines
        context = "\n".join(lines[max(0, stdout_line_idx - 5) : stdout_line_idx + 1])
        assert "inSandbox" in context, f"stdout.write fallback must be guarded by inSandbox, context:\n{context}"

    def test_sandbox_detected_once_at_startup(self):
        """detectSandbox() must exist and inSandbox must be set once at startup,
        not re-evaluated on every setTitle call.
        """
        text = _plugin_text()

        # detectSandbox function must exist (sync — no async needed for fs check)
        detect_body = _function_body(text, "const detectSandbox = ")
        assert detect_body, "detectSandbox function not found in plugin"

        # Must check filesystem for Screen socket (silent — no screen subprocess output)
        assert "existsSync" in detect_body or "statSync" in detect_body or "accessSync" in detect_body, (
            "detectSandbox must use synchronous fs check (existsSync/statSync/accessSync) "
            "to avoid screen subprocess output appearing in OpenCode TUI"
        )

        # inSandbox must be assigned from detectSandbox result (sync call, no await)
        assert "inSandbox = detectSandbox()" in text, "inSandbox must be set once via 'detectSandbox()' (synchronous)"

    def test_applyTitleNow_uses_screen_title_outside_sandbox(self):
        """Outside sandbox, applyTitleNow must use screen -X title."""
        text = _plugin_text()
        body = _function_body(text, "const applyTitleNow = async")
        assert body, "applyTitleNow function not found in plugin"

        # Old cache variable must not exist
        assert "screenTitleWorks" not in body, (
            "screenTitleWorks cache must not exist — transient failures must not "
            "permanently disable screen -X title outside sandbox"
        )

        # screen -X title must be called when !inSandbox
        assert "!inSandbox" in body, "applyTitleNow must call screen -X title when !inSandbox"
        assert "clearScreenHstatus()" in body, (
            "applyTitleNow must clear per-window hstatus before screen -X title to avoid shell prompt garbage"
        )

    def test_clearScreenHstatus_uses_nonempty_argument(self):
        """`screen -X hstatus` requires exactly one argument; empty string fails with
        'one argument required'. Use a single blank placeholder instead.
        """
        text = _plugin_text()
        body = _function_body(text, "const clearScreenHstatus = async")
        assert body, "clearScreenHstatus function not found in plugin"
        assert 'hstatus ${" "}' in body, (
            "clearScreenHstatus must pass a non-empty blank placeholder to screen -X hstatus"
        )

    def test_scheduleTitle_uses_debounce_and_last_title_cache(self):
        """scheduleTitle must use debounce and lastTitle cache to avoid redraw
        storms from frequent event bursts.
        """
        text = _plugin_text()

        # lastTitle cache variable must exist
        assert "lastTitle" in text, "lastTitle cache variable must exist in plugin"
        assert "TITLE_DEBOUNCE_MS" in text, "title debounce constant must exist in plugin"
        assert "titleTimer" in text, "title scheduler timer must exist in plugin"
        assert "HSTATUS_SCRUB_DELAY_MS" in text, "hstatus scrub delay constant must exist in plugin"
        assert "HSTATUS_SCRUB_PASSES" in text, "hstatus scrub pass count must exist in plugin"

        assert "const scheduleTitle = (emojiTitle, asciiTitle, { immediate = false } = {}) => {" in text, (
            "scheduleTitle function must exist in plugin"
        )
        assert "current === lastTitle" in text, "scheduleTitle must check lastTitle cache before scheduling"
        assert "setTimeout" in text, "scheduleTitle must debounce updates via setTimeout"
        assert "TITLE_DEBOUNCE_MS" in text, "scheduleTitle must use TITLE_DEBOUNCE_MS"

    def test_hstatus_cleanup_uses_short_pulses_not_permanent_interval(self):
        """Hstatus cleanup should be a short burst around title changes, not a
        permanent per-second interval that makes the status line visibly jump.
        """
        text = _plugin_text()
        assert "const pulseScreenHstatusCleanup = () => {" in text, (
            "pulseScreenHstatusCleanup function must exist in plugin"
        )
        assert "hstatusTimer" not in text, "plugin must not keep a permanent hstatus interval timer"
        assert "clearHstatusPulseTimers()" in text, "plugin must clear pending hstatus pulse timers"

    def test_startup_title_setup_is_deferred(self):
        """Startup title work must be done inside setImmediate, not synchronously
        during plugin initialisation.
        """
        text = _plugin_text()
        assert "setImmediate(async () => {" in text, "startup title setup must be deferred via setImmediate"
        assert "dynamictitle off" in text, "startup setup must disable dynamictitle outside sandbox"
        assert "clearScreenHstatus()" in text, "startup setup must clear per-window hstatus garbage in Screen"
        assert "pulseScreenHstatusCleanup()" in text, "startup setup must schedule hstatus cleanup pulses"

    def test_tool_execute_before_does_not_schedule_title(self):
        """tool.execute.before must not touch the title — only report state.
        This avoids screen redraws during active TUI rendering.
        """
        text = _plugin_text()
        body = _function_body(text, '"tool.execute.before": async')
        assert body, "tool.execute.before handler not found in plugin"
        assert "scheduleTitle" not in body, "tool.execute.before must not schedule title updates"
        assert "applyTitleNow" not in body, "tool.execute.before must not update title directly"
        assert "isIdle = false" in body, "tool.execute.before must clear idle flag before reporting running"
        assert 'reportState("running")' in body, "tool.execute.before must still report running state"

    def test_dispose_clears_pending_title_timer(self):
        """server.instance.disposed must clear any pending title timer before cleanup."""
        text = _plugin_text()
        assert "clearTitleTimer()" in text, "dispose path must clear pending title timer"
        assert "desiredTitle = null" in text, "dispose path must discard pending title request"
        assert "clearHstatusPulseTimers()" in text, "dispose path must clear pending hstatus cleanup pulses"

    def test_dispose_does_not_force_title_reset_escape_sequence(self):
        text = _plugin_text()
        body = _function_body(text, 'if (event.type === "server.instance.disposed")')
        assert body, "server.instance.disposed branch not found in plugin"
        assert 'applyTitleNow("", projectName)' not in body, (
            "dispose path must not emit an explicit title reset escape sequence during shutdown"
        )

    def test_dispose_uses_fire_and_forget_cleanup(self):
        text = _plugin_text()
        assert "const fireAndForget = (promise, label) => {" in text, "plugin must define fire-and-forget helper"
        body = _function_body(text, 'if (event.type === "server.instance.disposed")')
        assert body, "server.instance.disposed branch not found in plugin"
        assert 'fireAndForget(runBridgeClient("unregister", registeredSessionID), "bridge-unregister")' in body, (
            "dispose path must not await unregister synchronously"
        )
        assert "fireAndForget(" in body and "agent-notify-end" in body, (
            "dispose path must send end notification without blocking shutdown"
        )

    def test_message_updated_no_longer_updates_title_immediately(self):
        """message.updated should only update currentAgent and rely on later state
        transitions for the title update.
        """
        text = _plugin_text()
        body = _function_body(text, 'if (event.type === "message.updated")')
        assert body, "message.updated branch not found in plugin"
        assert "scheduleTitle" not in body, (
            "message.updated must not schedule title directly — avoid extra redraws during render burst"
        )

    def test_session_status_busy_uses_immediate_title(self):
        """Busy state must update the title immediately so short tool calls still
        become visible to the user.
        """
        text = _plugin_text()
        body = _function_body(text, 'if (event.type === "session.status")')
        assert body, "session.status branch not found in plugin"
        assert "immediate: true" in body, "session.status busy must use immediate title update"

    def test_permission_asked_uses_immediate_title(self):
        """Permission dialog must flip to red immediately — delayed debounce would
        hide or postpone the security signal.
        """
        text = _plugin_text()
        body = _function_body(text, 'if (event.type === "permission.asked")')
        assert body, "permission.asked branch not found in plugin"
        assert "immediate: true" in body, "permission.asked must use immediate title update"


# ---------------------------------------------------------------------------
# TestPluginNothrowOnExternalCalls
# ---------------------------------------------------------------------------


class TestPluginNothrowOnExternalCalls:
    """Every bun shell $`...` call must use .nothrow() to prevent unhandled
    exceptions from missing external tools in sandbox environments.
    """

    def test_all_dollar_template_calls_use_nothrow(self):
        """Every $`...` bun shell template literal must be followed by .nothrow().

        This ensures that missing tools (screen, agent-notify.sh, test) do not
        throw exceptions and crash the plugin in a sandbox.
        """
        text = _plugin_text()
        lines = text.splitlines()

        # Find lines with bun shell template calls (excluding comments and the
        # CLIENT_BIN wrapper which is inside runClient and already guarded)
        dollar_lines = [
            (i + 1, line)
            for i, line in enumerate(lines)
            if re.search(r"\$`", line) and not line.strip().startswith("//")
        ]
        assert dollar_lines, "No $`...` calls found — plugin structure may have changed"

        missing_nothrow = [(lineno, line.strip()) for lineno, line in dollar_lines if ".nothrow()" not in line]
        assert not missing_nothrow, (
            "The following $`...` calls are missing .nothrow() — "
            "they will throw in sandbox if the tool is missing:\n"
            + "\n".join(f"  line {n}: {ln}" for n, ln in missing_nothrow)
        )

    def test_agent_notify_calls_use_nothrow(self):
        """agent-notify helper must no longer run through raw Bun shell templates."""
        text = _plugin_text()
        assert "const runAgentNotify = async" in text, "runAgentNotify helper must exist"
        assert "$`${process.env.HOME}/claude-home/agent-notify.sh" not in text, (
            "agent-notify shell template calls must be removed in favor of quiet spawned execution"
        )


# ---------------------------------------------------------------------------
# TestPluginNoopWhenNoSession
# ---------------------------------------------------------------------------


class TestPluginNoopWhenNoSession:
    """Key functions must return immediately when no session is registered,
    preventing any HTTP or socket calls in sandbox mode where CLIENT_BIN=null
    and registeredSessionID stays null.
    """

    def test_pollInbox_has_registeredSessionID_guard(self):
        """pollInbox must check registeredSessionID before making any HTTP call.

        In sandbox mode registeredSessionID stays null (register is a noop),
        so pollInbox must return immediately without touching the network.
        """
        text = _plugin_text()
        body = _function_body(text, "const pollInbox = async")
        assert body, "pollInbox function not found in plugin"

        assert "!registeredSessionID" in body, "pollInbox must check !registeredSessionID as a guard"
        # The guard must be near the top — within the first 3 lines of the body
        inner = body.strip().lstrip("{").strip()
        first_lines = "\n".join(inner.splitlines()[:3])
        assert "!registeredSessionID" in first_lines, (
            "pollInbox registeredSessionID guard must be in the first 3 lines of the function body, "
            f"got:\n{first_lines}"
        )

    def test_reregTimer_callback_has_registeredSessionID_guard(self):
        """The periodic re-register timer callback must check registeredSessionID
        and return immediately if null — no bridge calls in sandbox mode.
        """
        text = _plugin_text()

        # Find the reregTimer setInterval block — search for the assignment
        idx = text.find("reregTimer = setInterval")
        assert idx != -1, "reregTimer setInterval not found in plugin"

        # Extract from the setInterval call to the closing ), REREG_INTERVAL_MS)
        # The callback is the async arrow function passed as first arg.
        # Find the opening brace of the callback body.
        brace_start = text.find("{", idx)
        assert brace_start != -1, "reregTimer callback opening brace not found"

        # Walk forward to find the matching closing brace of the callback
        depth = 0
        callback_body = ""
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    callback_body = text[brace_start : i + 1]
                    break
        assert callback_body, "Could not extract reregTimer callback body"

        assert "!registeredSessionID" in callback_body, (
            f"reregTimer callback must check !registeredSessionID and return early\nCallback body: {callback_body!r}"
        )
        assert "return" in callback_body, "reregTimer callback must return early when registeredSessionID is null"

    def test_reportState_returns_immediately_without_session(self):
        """reportState must return true (= failed) immediately when
        registeredSessionID is null, without calling runClient.
        """
        text = _plugin_text()
        body = _function_body(text, "const reportState = async")
        assert body, "reportState function not found in plugin"

        assert "!registeredSessionID" in body, "reportState must check !registeredSessionID"
        # The guard must be the first statement
        inner = body.strip().lstrip("{").strip()
        inner_no_comments = re.sub(r"//[^\n]*", "", inner).strip()
        assert inner_no_comments.startswith("if (!registeredSessionID)"), (
            f"reportState first statement must be 'if (!registeredSessionID)', got: {inner_no_comments[:80]!r}"
        )


class TestPushBasedDelivery:
    """v0.9.0: push-based delivery via prompt_async replaces polling+rawRelay."""

    def test_no_rawRelay_function(self):
        """rawRelay was removed in v0.9.0 — plugin must not contain it."""
        text = _plugin_text()
        body = _function_body(text, "const rawRelay = async")
        assert not body, "rawRelay function must not exist in v0.9.0+ plugin"

    def test_no_messageBuffer(self):
        """messageBuffer was removed — all messages injected at once."""
        text = _plugin_text()
        assert "let messageBuffer" not in text, "messageBuffer variable must not exist"

    def test_no_polling_timer(self):
        """30s polling timer was removed — session.idle is the only trigger."""
        text = _plugin_text()
        assert "IDLE_POLL_INTERVAL_MS" not in text, "IDLE_POLL_INTERVAL_MS must not exist"
        assert "let pollTimer" not in text, "pollTimer variable must not exist"

    def test_no_idle_delay(self):
        """1.5s delay before pollInbox was removed."""
        text = _plugin_text()
        # The old pattern: setTimeout(resolve, 1500)
        assert "setTimeout(resolve, 1500)" not in text, "1.5s idle delay must not exist"

    def test_injectViaPromptAsync_uses_prompt_async_endpoint(self):
        """Push delivery must use OpenCode HTTP API prompt_async."""
        text = _plugin_text()
        body = _function_body(text, "const injectViaPromptAsync = async")
        assert body, "injectViaPromptAsync function must exist"
        assert "prompt_async" in body, "must target prompt_async endpoint"
        assert "fetch(" in body, "must use HTTP fetch"
        assert '"POST"' in body, "must use POST method"
        assert "parts" in body, "must send parts array"

    def test_pollInbox_uses_injectViaPromptAsync(self):
        """pollInbox must use injectViaPromptAsync instead of rawRelay."""
        text = _plugin_text()
        body = _function_body(text, "const pollInbox = async")
        assert body, "pollInbox function must exist"
        assert "injectViaPromptAsync" in body, "pollInbox must call injectViaPromptAsync"
        assert "rawRelay" not in body, "pollInbox must not reference rawRelay"
        assert "messageBuffer" not in body, "pollInbox must not reference messageBuffer"

    def test_pollInbox_no_sty_guard(self):
        """pollInbox must work without Screen (prompt_async is backend-agnostic)."""
        text = _plugin_text()
        body = _function_body(text, "const pollInbox = async")
        assert body, "pollInbox function must exist"
        # The old guard was: !STY
        assert "!STY" not in body, "pollInbox must not require STY (Screen)"

    def test_opencodeSessionID_tracked(self):
        """Plugin must track raw OpenCode session ID for prompt_async."""
        text = _plugin_text()
        assert "let opencodeSessionID = null" in text, "opencodeSessionID must be declared"
        assert "opencodeSessionID = active.id" in text, "must set from startup registration"
        assert "opencodeSessionID = info.id" in text, "must set from session.created"

    def test_serverUrl_resolved(self):
        """Plugin must resolve OpenCode server URL for HTTP API calls."""
        text = _plugin_text()
        assert "serverUrl" in text, "serverUrl must be defined"
        assert "OPENCODE_SERVER_URL" in text, "must support env var override"
        assert "localhost:4096" in text, "must have sensible default"
