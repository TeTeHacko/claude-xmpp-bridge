"""Delivery read-back: send_text's True only means screen accepted the command.

On 2026-10-21 every relay message over ~550 bytes vanished while the log said
"Stuffed" - these tests pin the check that would have caught it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from helpers import make_mock_conn as _make_mock_conn

from claude_xmpp_bridge.bridge import XMPPBridge
from claude_xmpp_bridge.config import Config
from claude_xmpp_bridge.multiplexer import ScreenMultiplexer, collapse_ws

_EXEC_PATCH = "claude_xmpp_bridge.multiplexer.asyncio.create_subprocess_exec"

INFO = {
    "sty": "123.pts-0.host",
    "window": "6",
    "project": "/home/u/proj",
    "backend": "screen",
}


def _config(tmp_path: Path) -> Config:
    return Config(
        jid="bot@example.com",
        password="secret",
        recipient="user@example.com",
        socket_path=tmp_path / "test.sock",
        db_path=tmp_path / "test.db",
        messages_file=None,
    )


def _hardcopy_writer(content: str):
    """create_subprocess_exec stand-in that writes *content* to the hardcopy path."""

    async def fake_exec(*args, **kwargs):
        path = Path(args[args.index("-h") + 1])
        path.write_text(content, encoding="utf-8")
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    return fake_exec


class TestCollapseWs:
    def test_wrapped_text_matches(self):
        assert collapse_ws("abc def\n  ghi") == collapse_ws("abcdef\nghi") == "abcdefghi"


class TestWindowContains:
    async def test_found_across_line_wrap(self):
        mux = ScreenMultiplexer()
        screen = '❯ {"type": "relay", "message_id": "ab12\n  cd34ef"}\n'
        with patch(_EXEC_PATCH, side_effect=_hardcopy_writer(screen)) as mock_exec:
            assert await mux.window_contains("s", "6", "ab12cd34ef") is True
        args = mock_exec.call_args.args
        assert args[:7] == ("screen", "-S", "s", "-p", "6", "-X", "hardcopy")

    async def test_missing(self):
        mux = ScreenMultiplexer()
        with patch(_EXEC_PATCH, side_effect=_hardcopy_writer("nothing here\n")):
            assert await mux.window_contains("s", "6", "ab12cd34ef") is False

    async def test_unreadable_is_none_not_false(self):
        mux = ScreenMultiplexer()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=1)
        proc.returncode = 1
        with patch(_EXEC_PATCH, new_callable=AsyncMock, return_value=proc):
            assert await mux.window_contains("s", "6", "x") is None

    async def test_rejects_bad_window(self):
        mux = ScreenMultiplexer()
        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            assert await mux.window_contains("s", "6;quit", "x") is None
        mock_exec.assert_not_called()

    async def test_tempfile_removed(self, tmp_path):
        mux = ScreenMultiplexer()
        seen: list[Path] = []
        inner = _hardcopy_writer("x")

        async def spy(*args, **kwargs):
            seen.append(Path(args[args.index("-h") + 1]))
            return await inner(*args, **kwargs)

        with patch(_EXEC_PATCH, side_effect=spy):
            await mux.window_contains("s", "6", "x")
        assert seen and not seen[0].exists() and not seen[0].parent.exists()


class TestDeliveryNeedle:
    def test_uses_message_id(self):
        text = '[bridge-generated message]\n{"type": "relay", "message_id": "4b69d50a02ae", "ts": 1}\n\nhello'
        assert XMPPBridge._delivery_needle(text) == "4b69d50a02ae"

    def test_falls_back_to_tail(self):
        text = "x" * 100 + " KONEC"
        assert XMPPBridge._delivery_needle(text) == ("x" * 100 + "KONEC")[-40:]


class TestVerifyDelivery:
    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_missing_alerts(self, MockXMPP, tmp_path):
        conn, _ = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_config(tmp_path))
        bridge._VERIFY_DELAY = 0
        bridge.audit = MagicMock()
        mux = MagicMock()
        mux.window_contains = AsyncMock(return_value=False)
        await bridge._verify_delivery(mux, "sid", INFO, '{"message_id": "abcdef123456"} body')
        events = [c.args[0] for c in bridge.audit.log.call_args_list]
        assert "TERMINAL_SEND_UNVERIFIED" in events
        conn.send.assert_called_once()
        assert "abcdef123456" in conn.send.call_args.args[1]
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_found_is_quiet(self, MockXMPP, tmp_path):
        conn, _ = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_config(tmp_path))
        bridge._VERIFY_DELAY = 0
        bridge.audit = MagicMock()
        mux = MagicMock()
        mux.window_contains = AsyncMock(return_value=True)
        await bridge._verify_delivery(mux, "sid", INFO, "hello world")
        assert bridge.audit.log.call_args.args[0] == "TERMINAL_SEND_VERIFIED"
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_unknown_does_not_alert(self, MockXMPP, tmp_path):
        conn, _ = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_config(tmp_path))
        bridge._VERIFY_DELAY = 0
        bridge.audit = MagicMock()
        mux = MagicMock()
        mux.window_contains = AsyncMock(return_value=None)
        await bridge._verify_delivery(mux, "sid", INFO, "hello world")
        bridge.audit.log.assert_not_called()
        conn.send.assert_not_called()
        bridge.registry.close()

    @patch("claude_xmpp_bridge.bridge.XMPPConnection")
    async def test_readback_exception_is_swallowed(self, MockXMPP, tmp_path):
        conn, _ = _make_mock_conn(MockXMPP)
        bridge = XMPPBridge(_config(tmp_path))
        bridge._VERIFY_DELAY = 0
        mux = MagicMock()
        mux.window_contains = AsyncMock(side_effect=OSError("boom"))
        await bridge._verify_delivery(mux, "sid", INFO, "hello world")
        conn.send.assert_not_called()
        bridge.registry.close()
