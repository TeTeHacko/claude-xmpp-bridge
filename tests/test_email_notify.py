"""Tests for claude_xmpp_bridge.email_notify."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from claude_xmpp_bridge.email_notify import send_email


class TestSendEmail:
    """Tests for send_email() async helper."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = await send_email(
                smtp_host="127.0.0.1",
                smtp_port=25,
                sender="bot@example.com",
                recipient="user@example.com",
                subject="Test",
                body="Hello world",
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_calls_sendmail_with_correct_args(self):
        captured: dict = {}

        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            def capture_sendmail(sender, recipients, msg_str):
                captured["sender"] = sender
                captured["recipients"] = recipients
                captured["msg"] = msg_str

            mock_smtp.sendmail = capture_sendmail

            await send_email(
                smtp_host="mailhost",
                smtp_port=587,
                sender="from@example.com",
                recipient="to@example.com",
                subject="Subject line",
                body="Body text",
            )

        import base64

        assert captured["sender"] == "from@example.com"
        assert captured["recipients"] == ["to@example.com"]
        assert "Subject line" in captured["msg"]
        # Body is base64-encoded in the MIME message
        assert base64.b64encode(b"Body text").decode() in captured["msg"].replace("\n", "")

    @pytest.mark.asyncio
    async def test_returns_false_on_smtp_error(self):
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("refused")):
            result = await send_email(
                smtp_host="bad-host",
                smtp_port=25,
                sender="bot@example.com",
                recipient="user@example.com",
                subject="Test",
                body="Body",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            result = await send_email(
                smtp_host="192.0.2.1",
                smtp_port=25,
                sender="bot@example.com",
                recipient="user@example.com",
                subject="Test",
                body="Body",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_smtp_opened_with_correct_host_port(self):
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            await send_email(
                smtp_host="192.168.33.200",
                smtp_port=25,
                sender="bot@example.com",
                recipient="user@example.com",
                subject="S",
                body="B",
            )

        mock_smtp_cls.assert_called_once_with("192.168.33.200", 25, timeout=10)

    @pytest.mark.asyncio
    async def test_message_has_auto_submitted_header(self):
        captured: dict = {}

        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            def capture(sender, recipients, msg_str):
                captured["msg"] = msg_str

            mock_smtp.sendmail = capture

            await send_email(
                smtp_host="h",
                smtp_port=25,
                sender="a@b.com",
                recipient="c@d.com",
                subject="S",
                body="B",
            )

        assert "Auto-Submitted: auto-generated" in captured["msg"]

    @pytest.mark.asyncio
    async def test_unicode_body_encoded_as_utf8(self):
        captured: dict = {}

        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            def capture(sender, recipients, msg_str):
                captured["msg"] = msg_str

            mock_smtp.sendmail = capture

            await send_email(
                smtp_host="h",
                smtp_port=25,
                sender="a@b.com",
                recipient="c@d.com",
                subject="S",
                body="Příliš žluťoučký kůň 🐴",
            )

        # charset=utf-8 must appear in the MIME headers
        assert "utf-8" in captured["msg"].lower()
