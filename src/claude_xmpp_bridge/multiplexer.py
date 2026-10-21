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
_TARGET_RE = re.compile(r"^[a-zA-Z0-9_.%\-]{1,128}$")

# Timeout (seconds) for each subprocess invocation.
_CMD_TIMEOUT = 5

# Delay (seconds) between text injection and CR/Enter to give readline time
# to process the pasted text before the Enter arrives.
_INTER_CMD_DELAY = 0.05

# GNU Screen silently drops a ``-X`` command whose message exceeds its internal
# buffer: ``screen -X at N# stuff <text>`` exits 0, nothing reaches the window,
# only the following CR arrives. Measured 2026-10-21 on screen 5.0.2: 746 bytes
# of text passed, 747 were dropped (bytes, not characters - UTF-8 counts double);
# through the bridge a 740-byte message was already lost (the limit covers the
# whole command, including the target and window arguments). Every relay message
# longer than ~550 characters of payload vanished while the log said "Stuffed".
# Text is therefore sent in chunks well below that; the limit applies to the
# *escaped* text, and escaping can double it, so raw chunks stay <= 256 bytes.
_SCREEN_CHUNK_BYTES = 256

# Bracketed-paste markers around the text (screen only). Claude Code treats
# input arriving in quick succession as one paste, and a CR that lands inside
# that paste does not submit: it becomes a newline or is dropped, and the text
# waits in the prompt until a human presses Enter (seen 2026-10-21 in real
# windows, even for a single ~650-byte stuff). Timing does not fix it - measured
# on a throwaway Claude Code session with an attached display: 3 chunks 50 ms
# apart never submitted, 0.3 s gaps before the CR submitted 1 of 3 messages.
# With the text wrapped in ESC[200~ ... ESC[201~ the app knows exactly where
# the paste ends, and the CR after it submitted 3 of 3. Requires a target that
# enables bracketed paste (Claude Code, OpenCode and bash readline all do).
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# CR/Enter delivery can fail transiently even after text injection already
# succeeded. Retry the keypress a few times so readline is more likely to submit
# the prompt and the message does not remain half-typed in the target session.
_KEYPRESS_RETRIES = 3


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


def _split_utf8(text: str, max_bytes: int) -> list[str]:
    """Split *text* into pieces of at most *max_bytes* UTF-8 bytes each.

    Splits only between characters, never inside a multi-byte sequence, so
    every piece is valid UTF-8 on its own. Escaping is applied per piece
    afterwards, which also keeps an escape pair (``\\\\``, ``\\$``) whole.
    """
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if current and size + n > max_bytes:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(ch)
        size += n
    if current:
        pieces.append("".join(current))
    return pieces


def _get_safe_env() -> dict[str, str]:
    """Return a minimal environment for safely executing subprocesses."""
    env = {}
    for var in ("PATH", "USER", "HOME", "LANG", "LC_ALL", "TERM", "SCREENDIR"):
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


async def _run_cmd_with_retries(*args: str, label: str, retries: int) -> bool:
    """Run a command and retry transient failures a limited number of times."""
    for attempt in range(1, retries + 1):
        if await _run_cmd(*args, label=label):
            return True
        if attempt < retries:
            await asyncio.sleep(_INTER_CMD_DELAY)
            log.warning("Retrying %s (%d/%d)", label, attempt + 1, retries)
    return False


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

        The text is wrapped in bracketed-paste markers and sent in chunks of at
        most ``_SCREEN_CHUNK_BYTES`` (screen silently drops longer ``-X``
        commands), and the CR goes out only after the paste-end marker, so the
        app treats it as Enter and not as part of the paste. See the comments
        at ``_SCREEN_CHUNK_BYTES`` and ``_PASTE_START`` for the measurements.
        """
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid screen target: %r", target)
            return False
        chunks = _split_utf8(sanitize_text(text), _SCREEN_CHUNK_BYTES)
        # The markers are added after sanitize_text, which would strip the ESC.
        for i, piece in enumerate([_PASTE_START, *map(_screen_stuff_escape, chunks), _PASTE_END]):
            if i:
                await asyncio.sleep(_INTER_CMD_DELAY)
            if not await _run_cmd(
                "screen",
                "-S",
                target,
                "-X",
                "at",
                f"{window}#",
                "stuff",
                piece,
                label="Screen stuff",
            ):
                return False

        await asyncio.sleep(_INTER_CMD_DELAY)
        if not await _run_cmd_with_retries(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            "\r",
            label="Screen CR",
            retries=_KEYPRESS_RETRIES,
        ):
            return False

        log.info("Stuffed to screen %s window %s (%d chunk(s))", target, window, len(chunks))
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
        if not await _run_cmd_with_retries(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            "\r",
            label="Screen nudge CR",
            retries=_KEYPRESS_RETRIES,
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
        if not await _run_cmd_with_retries(
            "tmux",
            "send-keys",
            "-t",
            target,
            "Enter",
            label="tmux Enter",
            retries=_KEYPRESS_RETRIES,
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
        if not await _run_cmd_with_retries(
            "tmux",
            "send-keys",
            "-t",
            target,
            "Enter",
            label="tmux nudge Enter",
            retries=_KEYPRESS_RETRIES,
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
