"""Terminal multiplexer backends (GNU Screen, tmux) with text sanitization."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Protocol

log = logging.getLogger(__name__)

# Remove ASCII control characters 0-31 except newline (0x0A), and DEL (0x7F)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

# Allowed characters in multiplexer target (session name).
# Colon is intentionally excluded to prevent tmux session:window injection.
_TARGET_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")

# Timeout (seconds) for each subprocess invocation.
_CMD_TIMEOUT = 5

# Delay (seconds) between text injection and CR/Enter to give readline time
# to process the pasted text before the Enter arrives.
_INTER_CMD_DELAY = 0.05


def sanitize_text(text: str) -> str:
    """Remove control characters from text, preserving newlines and unicode."""
    return _CONTROL_CHARS_RE.sub("", text)


def _screen_stuff_escape(text: str) -> str:
    """Escape characters that GNU Screen's ``stuff`` command interprets.

    Screen's command parser expands ``$VAR`` as environment variables (or empty
    string if unset) and interprets C-style backslash sequences (``\\n``,
    ``\\r``, ``\\t``, etc.).  To preserve the literal text we escape ``\\`` →
    ``\\\\`` (must be first) and ``$`` → ``\\$``.
    """
    return text.replace("\\", "\\\\").replace("$", "\\$")


def _get_safe_env() -> dict[str, str]:
    """Return a minimal environment for safely executing subprocesses."""
    env = {}
    for var in ("PATH", "USER", "HOME", "LANG", "LC_ALL", "TERM"):
        if var in os.environ:
            env[var] = os.environ[var]
    return env


async def _run_cmd(*args: str, label: str) -> bool:
    """Run a subprocess with timeout and safe environment.

    Returns True on success (exit code 0), False on failure or timeout.
    *label* is used in log messages to identify the operation.
    """
    proc = await asyncio.create_subprocess_exec(*args, env=_get_safe_env())
    try:
        if await asyncio.wait_for(proc.wait(), timeout=_CMD_TIMEOUT) != 0:
            log.error("%s failed (exit %d)", label, proc.returncode)
            return False
    except TimeoutError:
        log.error("%s timed out", label)
        proc.kill()
        await proc.wait()
        return False
    return True


class Multiplexer(Protocol):
    """Protocol for terminal multiplexer backends."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a multiplexer session. Returns True on success."""
        ...

    async def send_nudge(self, target: str, window: str) -> bool:
        """Send only a CR to wake up the agent (nudge pattern). Returns True on success."""
        ...


class ScreenMultiplexer:
    """GNU Screen backend — sends text via at N# stuff."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a GNU Screen window.

        Uses 'at N# stuff <text>' + 'at N# stuff \\r' instead of plain 'stuff'
        or 'register + paste'.
        Rejects targets that don't match the allowed pattern (no shell metacharacters).

        Plain 'stuff' always targets the currently-focused window, ignoring any
        -p selector.  'register + paste' routes to the correct window but 'paste'
        writes raw bytes to the PTY buffer, which readline-based apps (like
        Claude Code running in Node.js raw mode) do not interpret as a key
        event — so \\r never triggers Enter.

        'at N#' changes the command's window context, causing 'stuff' to route
        to the target window regardless of focus.  Because 'stuff' goes through
        screen's key-event pipeline (not the raw PTY write path), readline
        correctly recognises the \\r as an Enter keypress.

        A short sleep between text and CR gives readline time to process the
        pasted text before the Enter arrives.
        """
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid screen target: %r", target)
            return False
        text = _screen_stuff_escape(sanitize_text(text))
        if not await _run_cmd(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            text,
            label="Screen stuff",
        ):
            return False

        await asyncio.sleep(_INTER_CMD_DELAY)
        if not await _run_cmd(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            "\r",
            label="Screen CR",
        ):
            return False

        log.info("Stuffed to screen %s window %s", target, window)
        return True

    async def send_nudge(self, target: str, window: str) -> bool:
        """Send only a CR to wake up the agent (nudge pattern).

        Unlike send_text, no message text is injected — only a bare CR is sent.
        This triggers readline's Enter handling and causes session.idle to fire,
        which prompts the plugin's pollInbox() to drain the MCP inbox.
        """
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid screen target: %r", target)
            return False
        if not await _run_cmd(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            "\r",
            label="Screen nudge CR",
        ):
            return False

        log.info("Nudged screen %s window %s", target, window)
        return True


class TmuxMultiplexer:
    """tmux backend — sends text via send-keys."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a tmux pane via send-keys."""
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid tmux target: %r", target)
            return False
        text = sanitize_text(text)
        if not await _run_cmd(
            "tmux",
            "send-keys",
            "-t",
            target,
            "-l",
            "--",
            text,
            label="tmux send-keys",
        ):
            return False

        await asyncio.sleep(_INTER_CMD_DELAY)
        if not await _run_cmd(
            "tmux",
            "send-keys",
            "-t",
            target,
            "Enter",
            label="tmux Enter",
        ):
            return False

        log.info("Sent to tmux pane %s", target)
        return True

    async def send_nudge(self, target: str, window: str) -> bool:
        """Send only Enter to wake up the agent (nudge pattern).

        Unlike send_text, no message text is injected — only a bare Enter is sent.
        This triggers readline's Enter handling and causes session.idle to fire,
        which prompts the plugin's pollInbox() to drain the MCP inbox.
        """
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid tmux target: %r", target)
            return False
        if not await _run_cmd(
            "tmux",
            "send-keys",
            "-t",
            target,
            "Enter",
            label="tmux nudge Enter",
        ):
            return False

        log.info("Nudged tmux pane %s", target)
        return True


def get_multiplexer(backend: str | None) -> Multiplexer | None:
    """Get the appropriate multiplexer backend."""
    if backend == "screen":
        return ScreenMultiplexer()
    if backend == "tmux":
        return TmuxMultiplexer()
    return None
