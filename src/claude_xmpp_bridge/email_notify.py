"""Async email notification helper.

Sends a plain-text email via a local (unauthenticated) SMTP relay.
Used by the bridge to deliver full message bodies when the XMPP
notification is truncated because it exceeds *email_threshold* characters.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import smtplib
from datetime import UTC, datetime
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


async def send_email(
    smtp_host: str,
    smtp_port: int,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> bool:
    """Send *body* via SMTP in a thread-pool executor (non-blocking).

    Args:
        smtp_host: SMTP relay hostname or IP.
        smtp_port: SMTP relay port (typically 25 or 587).
        sender:    From address (envelope and header).
        recipient: To address.
        subject:   Email subject line.
        body:      Full plain-text body.

    Returns:
        True on success, False if delivery failed (exception is logged).
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            _send_sync,
            smtp_host,
            smtp_port,
            sender,
            recipient,
            subject,
            body,
        )
        log.debug("Email sent to %s via %s:%d", recipient, smtp_host, smtp_port)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Email delivery failed: %s", exc)
        return False


def _send_sync(
    smtp_host: str,
    smtp_port: int,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    """Synchronous SMTP send — called from a thread-pool executor."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(datetime.now(tz=UTC))
    msg["Auto-Submitted"] = "auto-generated"

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
        smtp.sendmail(sender, [recipient], msg.as_string())
