"""Terminal multiplexer backends (GNU Screen, tmux) with text sanitization."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

# Remove ASCII control characters 0-31 except newline (0x0A), and DEL (0x7F)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

# Allowed characters in multiplexer target (session name).
# Colon is intentionally excluded to prevent tmux session:window injection.
_TARGET_RE = re.compile(r"^[a-zA-Z0-9_.\-]{0,128}$")


def sanitize_text(text: str) -> str:
    """Remove control characters from text, preserving newlines and unicode."""
    return _CONTROL_CHARS_RE.sub("", text)


class Multiplexer(Protocol):
    """Protocol for terminal multiplexer backends."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a multiplexer session. Returns True on success."""
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

        A 50 ms sleep between text and CR gives readline time to process the
        pasted text before the Enter arrives.
        """
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid screen target: %r", target)
            return False
        text = sanitize_text(text)
        proc1 = await asyncio.create_subprocess_exec(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            text,
        )
        try:
            if await asyncio.wait_for(proc1.wait(), timeout=5) != 0:
                log.error("Screen stuff failed (exit %d)", proc1.returncode)
                return False
        except TimeoutError:
            log.error("Screen stuff timed out")
            proc1.kill()
            await proc1.wait()
            return False

        await asyncio.sleep(0.05)
        proc2 = await asyncio.create_subprocess_exec(
            "screen",
            "-S",
            target,
            "-X",
            "at",
            f"{window}#",
            "stuff",
            "\r",
        )
        try:
            if await asyncio.wait_for(proc2.wait(), timeout=5) != 0:
                log.error("Screen CR failed (exit %d)", proc2.returncode)
                return False
        except TimeoutError:
            log.error("Screen CR timed out")
            proc2.kill()
            await proc2.wait()
            return False

        log.info("Stuffed to screen %s window %s", target, window)
        return True


class TmuxMultiplexer:
    """tmux backend — sends text via send-keys."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a tmux pane via send-keys."""
        if not _TARGET_RE.match(target):
            log.error("Rejected invalid tmux target: %r", target)
            return False
        text = sanitize_text(text)
        proc1 = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            target,
            "-l",
            "--",
            text,
        )
        try:
            if await asyncio.wait_for(proc1.wait(), timeout=5) != 0:
                log.error("tmux send-keys failed (exit %d)", proc1.returncode)
                return False
        except TimeoutError:
            log.error("tmux send-keys timed out")
            proc1.kill()
            await proc1.wait()
            return False

        await asyncio.sleep(0.05)
        proc2 = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            target,
            "Enter",
        )
        try:
            if await asyncio.wait_for(proc2.wait(), timeout=5) != 0:
                log.error("tmux Enter failed (exit %d)", proc2.returncode)
                return False
        except TimeoutError:
            log.error("tmux Enter timed out")
            proc2.kill()
            await proc2.wait()
            return False

        log.info("Sent to tmux pane %s", target)
        return True


def get_multiplexer(backend: str | None) -> Multiplexer | None:
    """Get the appropriate multiplexer backend."""
    if backend == "screen":
        return ScreenMultiplexer()
    if backend == "tmux":
        return TmuxMultiplexer()
    return None
