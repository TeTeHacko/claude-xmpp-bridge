"""Tests for terminal multiplexer backends and text sanitization."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from claude_xmpp_bridge.multiplexer import (
    ScreenMultiplexer,
    TmuxMultiplexer,
    get_multiplexer,
    sanitize_text,
)

# ---------------------------------------------------------------------------
# sanitize_text
# ---------------------------------------------------------------------------


class TestSanitizeText:
    """Tests for sanitize_text — control character removal."""

    def test_removes_null_byte(self):
        assert sanitize_text("hello\x00world") == "helloworld"

    def test_removes_low_control_chars(self):
        # 0x01 (SOH) through 0x09 (TAB) should be stripped
        text = "a\x01b\x02c\x03d\x04e\x05f\x06g\x07h\x08i\x09j"
        assert sanitize_text(text) == "abcdefghij"

    def test_removes_high_control_chars(self):
        # 0x0B (VT) through 0x1F (US) should be stripped
        text = "a\x0bb\x0cc\x0dd\x1ee\x1ff"
        assert sanitize_text(text) == "abcdef"

    def test_preserves_newline(self):
        assert sanitize_text("line1\nline2\n") == "line1\nline2\n"

    def test_preserves_normal_text(self):
        text = "Hello, World! 123 @#$%"
        assert sanitize_text(text) == text

    def test_preserves_unicode(self):
        text = "Ahoj svete! Prilis zlutoucky kun."
        assert sanitize_text(text) == text

    def test_empty_string(self):
        assert sanitize_text("") == ""

    def test_only_control_chars(self):
        assert sanitize_text("\x00\x01\x02\x03") == ""

    def test_mixed_content(self):
        assert sanitize_text("ok\x00\nnext\x07line") == "ok\nnextline"


# ---------------------------------------------------------------------------
# Helpers for subprocess mocking
# ---------------------------------------------------------------------------


_EXEC_PATCH = "claude_xmpp_bridge.multiplexer.asyncio.create_subprocess_exec"


def _make_process_mock(returncode: int = 0) -> AsyncMock:
    """Create a mock subprocess process with given returncode."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    return proc


def _make_timeout_process() -> AsyncMock:
    """Create a mock subprocess whose wait() raises TimeoutError."""
    proc = AsyncMock()
    proc.returncode = None

    async def _wait_forever():
        raise TimeoutError

    proc.wait = AsyncMock(side_effect=_wait_forever)
    return proc


# ---------------------------------------------------------------------------
# ScreenMultiplexer
# ---------------------------------------------------------------------------


class TestScreenMultiplexer:
    """Tests for ScreenMultiplexer.send_text."""

    async def test_success(self):
        """Both stuff and CR calls succeed (exit 0) -> returns True."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            result = await mux.send_text("session", "3", "hello world")

        assert result is True
        assert mock_exec.call_count == 2

        # First call: stuff with text
        first_call = mock_exec.call_args_list[0]
        assert first_call.args[:6] == ("screen", "-S", "session", "-p", "3", "-X")

        # Second call: stuff with CR
        second_call = mock_exec.call_args_list[1]
        assert 'stuff "\\015"' in second_call.args

    async def test_failure_first_call(self):
        """First screen command fails -> returns False immediately."""
        mux = ScreenMultiplexer()
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_fail
            result = await mux.send_text("session", "0", "text")

        assert result is False
        # Should bail out after the first call
        assert mock_exec.call_count == 1

    async def test_failure_second_call(self):
        """First call succeeds, second (CR) fails -> returns False."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [proc_ok, proc_fail]
            result = await mux.send_text("session", "0", "text")

        assert result is False
        assert mock_exec.call_count == 2

    async def test_timeout(self):
        """Subprocess wait times out -> returns False."""
        mux = ScreenMultiplexer()
        proc_timeout = _make_timeout_process()

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_timeout
            result = await mux.send_text("session", "0", "text")

        assert result is False

    async def test_sanitizes_input(self):
        """Control characters are stripped before sending to screen."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            await mux.send_text("s", "0", "clean\x00text")

        first_call = mock_exec.call_args_list[0]
        stuff_arg = first_call.args[-1]
        assert "\x00" not in stuff_arg
        assert "cleantext" in stuff_arg


# ---------------------------------------------------------------------------
# TmuxMultiplexer
# ---------------------------------------------------------------------------


class TestTmuxMultiplexer:
    """Tests for TmuxMultiplexer.send_text."""

    async def test_success(self):
        """Both send-keys calls succeed (exit 0) -> returns True."""
        mux = TmuxMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            result = await mux.send_text("session:0.1", "0", "hello world")

        assert result is True
        assert mock_exec.call_count == 2

        # First call: send-keys with text
        first_call = mock_exec.call_args_list[0]
        assert first_call.args[:2] == ("tmux", "send-keys")
        assert "hello world" in first_call.args

        # Second call: send-keys with Enter
        second_call = mock_exec.call_args_list[1]
        assert "Enter" in second_call.args

    async def test_failure_first_call(self):
        """First tmux command fails -> returns False immediately."""
        mux = TmuxMultiplexer()
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_fail
            result = await mux.send_text("session:0.1", "0", "text")

        assert result is False
        assert mock_exec.call_count == 1

    async def test_failure_second_call(self):
        """First call succeeds, second (Enter) fails -> returns False."""
        mux = TmuxMultiplexer()
        proc_ok = _make_process_mock(0)
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [proc_ok, proc_fail]
            result = await mux.send_text("session:0.1", "0", "text")

        assert result is False
        assert mock_exec.call_count == 2

    async def test_timeout(self):
        """Subprocess wait times out -> returns False."""
        mux = TmuxMultiplexer()
        proc_timeout = _make_timeout_process()

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_timeout
            result = await mux.send_text("session:0.1", "0", "text")

        assert result is False

    async def test_sanitizes_input(self):
        """Control characters are stripped before sending to tmux."""
        mux = TmuxMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            await mux.send_text("s", "0", "clean\x07text")

        first_call = mock_exec.call_args_list[0]
        # The text argument is the last positional arg in the first call
        assert "\x07" not in first_call.args
        assert "cleantext" in first_call.args


# ---------------------------------------------------------------------------
# get_multiplexer
# ---------------------------------------------------------------------------


class TestGetMultiplexer:
    """Tests for get_multiplexer factory function."""

    def test_screen_backend(self):
        result = get_multiplexer("screen")
        assert isinstance(result, ScreenMultiplexer)

    def test_tmux_backend(self):
        result = get_multiplexer("tmux")
        assert isinstance(result, TmuxMultiplexer)

    def test_none_backend(self):
        result = get_multiplexer(None)
        assert result is None

    def test_unknown_backend(self):
        result = get_multiplexer("unknown")
        assert result is None
