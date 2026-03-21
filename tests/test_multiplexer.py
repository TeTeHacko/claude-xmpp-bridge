"""Tests for terminal multiplexer backends and text sanitization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from claude_xmpp_bridge.multiplexer import (
    _KEYPRESS_RETRIES,
    ScreenMultiplexer,
    TmuxMultiplexer,
    _screen_stuff_escape,
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
# _screen_stuff_escape
# ---------------------------------------------------------------------------


class TestScreenStuffEscape:
    """Tests for _screen_stuff_escape — GNU Screen $VAR and backslash escaping."""

    def test_plain_text_unchanged(self):
        assert _screen_stuff_escape("hello world") == "hello world"

    def test_dollar_escaped(self):
        assert _screen_stuff_escape("$HOME") == r"\$HOME"

    def test_multiple_dollars(self):
        assert _screen_stuff_escape("$USER and $HOME") == r"\$USER and \$HOME"

    def test_backslash_escaped(self):
        assert _screen_stuff_escape("back\\slash") == "back\\\\slash"

    def test_backslash_before_dollar(self):
        # backslash must be escaped first to avoid double-escaping
        # input: \$ → output: \\\$ (escaped backslash + escaped dollar)
        assert _screen_stuff_escape("\\$HOME") == "\\\\\\$HOME"

    def test_empty_string(self):
        assert _screen_stuff_escape("") == ""

    def test_no_special_chars(self):
        text = "FANOUT w1: 5050"
        assert _screen_stuff_escape(text) == text

    def test_pipe_and_quotes_unchanged(self):
        # | and ' are NOT screen metacharacters in stuff — only $ and \ are
        text = "cmd | grep 'foo'"
        assert _screen_stuff_escape(text) == text

    def test_real_world_message(self):
        msg = "send $BRIDGE_SESSION_ID to /home/user\\config"
        expected = r"send \$BRIDGE_SESSION_ID to /home/user\\config"
        assert _screen_stuff_escape(msg) == expected


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
    """Create a mock subprocess whose first wait() raises TimeoutError, second succeeds."""
    proc = AsyncMock()
    proc.returncode = None
    proc.kill = MagicMock()

    _call_count = 0

    async def _wait_side_effect():
        nonlocal _call_count
        _call_count += 1
        if _call_count == 1:
            raise TimeoutError
        return None

    proc.wait = _wait_side_effect
    return proc


# ---------------------------------------------------------------------------
# ScreenMultiplexer
# ---------------------------------------------------------------------------


class TestScreenMultiplexer:
    """Tests for ScreenMultiplexer.send_text."""

    async def test_success(self):
        """Two calls: at N# stuff text, then at N# stuff \\r -> returns True."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            result = await mux.send_text("session", "3", "hello world")

        assert result is True
        assert mock_exec.call_count == 2

        # First call: screen -S session -X at 3# stuff "hello world"
        text_args = mock_exec.call_args_list[0].args
        assert text_args[:5] == ("screen", "-S", "session", "-X", "at")
        assert text_args[5] == "3#"
        assert text_args[6] == "stuff"
        assert text_args[7] == "hello world"

        # Second call: screen -S session -X at 3# stuff "\r"
        cr_args = mock_exec.call_args_list[1].args
        assert cr_args[:5] == ("screen", "-S", "session", "-X", "at")
        assert cr_args[5] == "3#"
        assert cr_args[6] == "stuff"
        assert cr_args[7] == "\r"

    async def test_uses_at_stuff_not_register_paste(self):
        """Must use at+stuff (not register+paste) so Enter is a key event."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            await mux.send_text("session", "4", "test")

        all_args = [a for call in mock_exec.call_args_list for a in call.args]
        assert "register" not in all_args
        assert "paste" not in all_args
        assert "stuff" in all_args
        assert "4#" in all_args

    async def test_failure_first_stuff_call(self):
        """First screen stuff command fails -> returns False, CR not sent."""
        mux = ScreenMultiplexer()
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_fail
            result = await mux.send_text("session", "0", "text")

        assert result is False
        assert mock_exec.call_count == 1

    async def test_failure_cr_stuff_call(self):
        """Text stuff succeeds, CR stuff keeps failing after retries -> False."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [proc_ok, *([proc_fail] * _KEYPRESS_RETRIES)]
            result = await mux.send_text("session", "0", "text")

        assert result is False
        assert mock_exec.call_count == 1 + _KEYPRESS_RETRIES

    async def test_cr_retry_eventually_succeeds(self):
        """If the first CR send fails transiently, the retry should recover."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [proc_ok, proc_fail, proc_ok]
            result = await mux.send_text("session", "0", "text")

        assert result is True
        assert mock_exec.call_count == 3

    async def test_timeout(self):
        """Subprocess wait times out -> returns False, kill() is called."""
        mux = ScreenMultiplexer()
        proc_timeout = _make_timeout_process()

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_timeout
            result = await mux.send_text("session", "0", "text")

        assert result is False
        proc_timeout.kill.assert_called_once()

    async def test_cr_sent_as_second_call(self):
        """CR must be sent as a separate stuff call, not appended to text."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            await mux.send_text("session", "0", "mytext")

        text_args = mock_exec.call_args_list[0].args
        cr_args = mock_exec.call_args_list[1].args
        # Text call must NOT contain \r
        assert "\r" not in text_args[-1]
        assert text_args[-1] == "mytext"
        # CR call must send exactly \r
        assert cr_args[-1] == "\r"

    async def test_sanitizes_input(self):
        """Control characters are stripped before sending to screen."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            await mux.send_text("s", "0", "clean\x00text")

        text_args = mock_exec.call_args_list[0].args
        assert "\x00" not in text_args[-1]
        assert "cleantext" in text_args[-1]

    async def test_rejects_invalid_target(self):
        """Target with shell metacharacters must be rejected immediately."""
        mux = ScreenMultiplexer()
        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            result = await mux.send_text("bad;target", "0", "text")
        assert result is False
        mock_exec.assert_not_called()

    async def test_rejects_del_char_in_text(self):
        """DEL (0x7F) must be stripped from text."""
        mux = ScreenMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            await mux.send_text("session", "0", "clean\x7ftext")

        text_args = mock_exec.call_args_list[0].args
        assert "\x7f" not in text_args[-1]
        assert "cleantext" in text_args[-1]


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
            result = await mux.send_text("mysession", "0", "hello world")

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
            result = await mux.send_text("mysession", "0", "text")

        assert result is False
        assert mock_exec.call_count == 1

    async def test_failure_second_call(self):
        """First call succeeds, Enter keeps failing even after retries -> False."""
        mux = TmuxMultiplexer()
        proc_ok = _make_process_mock(0)
        proc_fail = _make_process_mock(1)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [proc_ok, *([proc_fail] * _KEYPRESS_RETRIES)]
            result = await mux.send_text("mySession-1", "0", "text")

        assert result is False
        assert mock_exec.call_count == 1 + _KEYPRESS_RETRIES

    async def test_timeout(self):
        """Subprocess wait times out -> returns False, kill() is called."""
        mux = TmuxMultiplexer()
        proc_timeout = _make_timeout_process()

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_timeout
            result = await mux.send_text("mySession-1", "0", "text")

        assert result is False
        proc_timeout.kill.assert_called_once()

    async def test_rejects_colon_in_target(self):
        """Target with colon (tmux session:window syntax) must be rejected."""
        mux = TmuxMultiplexer()
        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            result = await mux.send_text("session:0.1", "0", "text")
        assert result is False
        mock_exec.assert_not_called()

    async def test_accepts_tmux_pane_id_with_percent(self):
        """Tmux pane IDs like %3 must be accepted by _TARGET_RE."""
        mux = TmuxMultiplexer()
        proc_ok = _make_process_mock(0)

        with patch(_EXEC_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = proc_ok
            result = await mux.send_text("%3", "0", "hello")

        assert result is True
        assert mock_exec.call_count >= 1

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
