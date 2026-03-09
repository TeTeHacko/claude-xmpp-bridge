"""Sandbox-safety invariants for the OpenCode plugin (xmpp-bridge.js).

These tests verify that the plugin is safe to run in a restricted environment
(bwrap sandbox, corporate network without bridge) by checking structural
invariants in the plugin source code.

Why static analysis instead of running the plugin:
  The project has no JavaScript test infrastructure.  The plugin runs inside
  the OpenCode process (embedded Bun runtime) — there is no easy way to unit-
  test it in isolation.  Static checks on the source are the next best thing:
  they catch regressions immediately and are fast (< 1 ms each).

Invariants checked:
  1. CLIENT_BIN fallback — runClient and rawRelay return immediately without
     any subprocess or console output when CLIENT_BIN is null.
  2. setTitle stdout fallback — title updates work via process.stdout.write
     even when screen socket is unavailable (bwrap --new-session).
  3. screenTitleWorks cache — after the first screen -X failure the flag is
     set to false and subsequent setTitle calls skip screen -X entirely.
  4. .nothrow() on every bun shell $`...` call — no unhandled exceptions from
     missing external tools (agent-notify.sh, test -f, screen -X).
  5. registeredSessionID guards — pollInbox, reregTimer callback, and
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
    """runClient and rawRelay must be no-ops when CLIENT_BIN is null."""

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

    def test_rawRelay_returns_silently_when_no_client_bin(self):
        """rawRelay must return immediately without spawning a process when
        CLIENT_BIN is null.
        """
        text = _plugin_text()
        body = _function_body(text, "const rawRelay = async")
        assert body, "rawRelay function not found in plugin"

        assert "!CLIENT_BIN" in body, "rawRelay must check !CLIENT_BIN as first guard"
        # Must not print anything to console
        assert "console.log" not in body, "rawRelay must not call console.log"
        assert "console.error" not in body, "rawRelay must not call console.error"

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

    def test_rawRelay_guard_is_first_statement(self):
        """The !CLIENT_BIN guard must be the very first statement in rawRelay."""
        text = _plugin_text()
        body = _function_body(text, "const rawRelay = async")
        assert body, "rawRelay function not found in plugin"

        inner = body.strip().lstrip("{").strip()
        inner_no_comments = re.sub(r"//[^\n]*", "", inner).strip()
        assert inner_no_comments.startswith("if (!CLIENT_BIN)"), (
            f"rawRelay first statement must be 'if (!CLIENT_BIN)', got: {inner_no_comments[:80]!r}"
        )


# ---------------------------------------------------------------------------
# TestPluginTitleFallback
# ---------------------------------------------------------------------------


class TestPluginTitleFallback:
    """setTitle must write to process.stdout when screen -X is unavailable."""

    def test_setTitle_has_stdout_escape_sequence_fallback(self):
        """setTitle must write ESC k ... ESC \\ to stdout as fallback for bwrap
        sandboxes where screen socket is inaccessible.
        """
        text = _plugin_text()
        body = _function_body(text, "const setTitle = async")
        assert body, "setTitle function not found in plugin"

        assert "process.stdout.write" in body, "setTitle must write to process.stdout as sandbox fallback"
        # Screen title escape: ESC k ... ESC backslash
        assert r"\x1bk" in body, r"setTitle stdout fallback must use \x1bk (Screen title escape sequence)"
        assert r"\x1b\\" in body, r"setTitle stdout fallback must close with \x1b\\ (Screen title terminator)"

    def test_setTitle_stdout_path_reachable_when_screen_fails(self):
        """The stdout.write path must be reachable when screenTitleWorks is false.

        Logic must be:
          if (STY && screenTitleWorks !== false) { screen -X ... }
          if (STY) { stdout.write ... }   ← reachable when screenTitleWorks=false
        """
        text = _plugin_text()
        body = _function_body(text, "const setTitle = async")
        assert body, "setTitle function not found in plugin"

        # The stdout.write for screen must NOT be inside an else branch of the
        # screenTitleWorks check — it must be a separate if(STY) block so it
        # runs even when screenTitleWorks is false.
        lines = body.splitlines()
        stdout_line_idx = next(
            (i for i, ln in enumerate(lines) if r"\x1bk" in ln),
            None,
        )
        assert stdout_line_idx is not None, r"No \x1bk line found in setTitle"

        # The stdout.write line must be preceded by `if (STY)` (not `else`)
        # within a few lines
        context = "\n".join(lines[max(0, stdout_line_idx - 5) : stdout_line_idx + 1])
        assert "if (STY)" in context, f"stdout.write fallback must be inside 'if (STY)' block, context:\n{context}"
        assert "} else {" not in context and "else if" not in context, (
            "stdout.write fallback must NOT be in an else branch — it must be reachable "
            "independently of the screenTitleWorks check"
        )

    def test_screenTitleWorks_set_to_false_on_screen_failure(self):
        """After screen -X fails, screenTitleWorks must be set to false so
        subsequent setTitle calls skip the screen -X attempt entirely.
        """
        text = _plugin_text()
        body = _function_body(text, "const setTitle = async")
        assert body, "setTitle function not found in plugin"

        # Must assign false to screenTitleWorks when screen -X fails
        assert "screenTitleWorks = false" in body, "setTitle must set screenTitleWorks = false when screen -X fails"

    def test_screenTitleWorks_checked_before_screen_call(self):
        """screen -X must only be called when screenTitleWorks !== false."""
        text = _plugin_text()
        body = _function_body(text, "const setTitle = async")
        assert body, "setTitle function not found in plugin"

        assert "screenTitleWorks !== false" in body, (
            "setTitle must check 'screenTitleWorks !== false' before calling screen -X"
        )


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
        """agent-notify.sh calls must use .nothrow() — the script may not exist
        in a sandbox or corporate environment.
        """
        text = _plugin_text()
        lines = text.splitlines()

        notify_lines = [
            (i + 1, line.strip())
            for i, line in enumerate(lines)
            if "agent-notify.sh" in line and not line.strip().startswith("//")
        ]
        assert notify_lines, "No agent-notify.sh calls found — plugin structure may have changed"

        missing = [(n, ln) for n, ln in notify_lines if ".nothrow()" not in ln]
        assert not missing, "agent-notify.sh calls missing .nothrow():\n" + "\n".join(
            f"  line {n}: {ln}" for n, ln in missing
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
