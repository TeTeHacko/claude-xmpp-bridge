"""Async email notification helper.

Sends a plain-text email via an SMTP relay.  When the relay is not
localhost, STARTTLS is attempted automatically to protect credentials
and message content in transit.

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

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


async def send_email(
    smtp_host: str,
    smtp_port: int,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    *,
    smtp_starttls: str = "auto",
) -> bool:
    """Send *body* via SMTP in a thread-pool executor (non-blocking).

    Args:
        smtp_host: SMTP relay hostname or IP.
        smtp_port: SMTP relay port (typically 25 or 587).
        sender:    From address (envelope and header).
        recipient: To address.
        subject:   Email subject line.
        body:      Full plain-text body.
        smtp_starttls: TLS mode — ``"auto"`` (default) uses STARTTLS for
            non-localhost hosts, ``"always"`` forces STARTTLS, ``"never"``
            disables it.

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
            smtp_starttls,
        )
        log.info("Email sent to %s via %s:%d", recipient, smtp_host, smtp_port)
        return True
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        log.warning("Email delivery failed: %s", exc)
        return False


def _send_sync(
    smtp_host: str,
    smtp_port: int,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    smtp_starttls: str = "auto",
) -> None:
    """Synchronous SMTP send — called from a thread-pool executor."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(datetime.now(tz=UTC))
    msg["Auto-Submitted"] = "auto-generated"

    use_tls = smtp_starttls == "always" or (
        smtp_starttls == "auto" and smtp_host.lower() not in _LOCALHOST_HOSTS
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.sendmail(sender, [recipient], msg.as_string())
