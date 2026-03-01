"""Terminal multiplexer backends (GNU Screen, tmux) with text sanitization."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

# Remove ASCII control characters 0-31 except newline (0x0A)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f]")


def sanitize_text(text: str) -> str:
    """Remove control characters from text, preserving newlines and unicode."""
    return _CONTROL_CHARS_RE.sub("", text)


class Multiplexer(Protocol):
    """Protocol for terminal multiplexer backends."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a multiplexer session. Returns True on success."""
        ...


class ScreenMultiplexer:
    """GNU Screen backend — sends text via the 'stuff' command."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a GNU Screen window via stuff command."""
        text = sanitize_text(text)
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        try:
            proc = await asyncio.create_subprocess_exec(
                "screen", "-S", target, "-p", window, "-X", "eval", f'stuff "{escaped}"',
            )
            if await asyncio.wait_for(proc.wait(), timeout=5) != 0:
                log.error("Screen stuff failed (exit %d)", proc.returncode)
                return False
            await asyncio.sleep(0.05)
            proc = await asyncio.create_subprocess_exec(
                "screen", "-S", target, "-p", window, "-X", "eval", 'stuff "\\015"',
            )
            if await asyncio.wait_for(proc.wait(), timeout=5) != 0:
                log.error("Screen CR failed (exit %d)", proc.returncode)
                return False
            log.info("Stuffed to screen %s window %s", target, window)
            return True
        except TimeoutError:
            log.error("Screen stuff timed out")
            return False


class TmuxMultiplexer:
    """tmux backend — sends text via send-keys."""

    async def send_text(self, target: str, window: str, text: str) -> bool:
        """Send text to a tmux pane via send-keys."""
        text = sanitize_text(text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", target, "-l", "--", text,
            )
            if await asyncio.wait_for(proc.wait(), timeout=5) != 0:
                log.error("tmux send-keys failed (exit %d)", proc.returncode)
                return False
            await asyncio.sleep(0.05)
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", target, "Enter",
            )
            if await asyncio.wait_for(proc.wait(), timeout=5) != 0:
                log.error("tmux Enter failed (exit %d)", proc.returncode)
                return False
            log.info("Sent to tmux pane %s", target)
            return True
        except TimeoutError:
            log.error("tmux send-keys timed out")
            return False


def get_multiplexer(backend: str | None) -> Multiplexer | None:
    """Get the appropriate multiplexer backend."""
    if backend == "screen":
        return ScreenMultiplexer()
    if backend == "tmux":
        return TmuxMultiplexer()
    return None
