"""Audit logger — structured JSON Lines event log for SIEM integration.

Supports two backends:
  - "journald": systemd.journal.JournalHandler (if available),
    fallback to SysLogHandler("/dev/log"), fallback to stderr.
  - Any other string: path to a rotating JSON Lines file
    (10 MB × 5 backups).

Each record is emitted as a single JSON object on one line::

    {"ts": "2026-03-05T14:32:01.123456Z", "event": "XMPP_IN", ...}

Configure via ``audit_log`` in config.toml or ``CLAUDE_XMPP_AUDIT_LOG``
environment variable.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from typing import Any

_AUDIT_LOGGER_NAME = "claude_xmpp_bridge.audit"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5


class _JsonLinesFormatter(logging.Formatter):
    """Pass-through formatter — the message is already a JSON string."""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def _build_journald_handler() -> logging.Handler:
    """Try systemd.journal, then SysLogHandler, then stderr."""
    try:
        from systemd.journal import JournalHandler  # type: ignore[import-not-found]

        handler: logging.Handler = JournalHandler(SYSLOG_IDENTIFIER="claude-xmpp-bridge")
        logging.getLogger(_AUDIT_LOGGER_NAME).debug("audit: using systemd JournalHandler")
        return handler
    except ImportError:
        pass

    try:
        handler = logging.handlers.SysLogHandler(address="/dev/log")
        logging.getLogger(_AUDIT_LOGGER_NAME).debug("audit: systemd not available, using SysLogHandler")
        return handler
    except OSError:
        pass

    logging.getLogger(_AUDIT_LOGGER_NAME).warning("audit: syslog not available, falling back to stderr")
    return logging.StreamHandler(sys.stderr)


class AuditLogger:
    """Emit structured audit events to journald or a rotating JSON Lines file."""

    def __init__(self, target: str) -> None:
        """Initialise the audit logger.

        Args:
            target: ``"journald"`` to use the system journal/syslog,
                or a filesystem path for a rotating JSON Lines file.
        """
        self._target = target
        self._logger = logging.getLogger(_AUDIT_LOGGER_NAME)
        self._logger.setLevel(logging.INFO)
        # Prevent propagation to the root logger — audit records should only
        # go to their dedicated handler, not pollute the main log stream.
        self._logger.propagate = False

        if target == "journald":
            handler = _build_journald_handler()
        else:
            handler = logging.handlers.RotatingFileHandler(
                target,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )

        handler.setFormatter(_JsonLinesFormatter())
        self._logger.addHandler(handler)
        self._handler = handler

    def log(self, event: str, **data: Any) -> None:
        """Emit one audit record.

        Args:
            event: Upper-case event identifier, e.g. ``"XMPP_IN"``.
            **data: Arbitrary key-value pairs included verbatim in the JSON
                object.  Values must be JSON-serialisable.
        """
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "event": event,
        }
        record.update(data)
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # Fallback: emit a safe record with the error instead of silently dropping
            fallback: dict[str, Any] = {
                "ts": record["ts"],
                "event": event,
                "audit_serialization_error": str(exc),
            }
            line = json.dumps(fallback, ensure_ascii=False)
        self._logger.info(line)

    def close(self) -> None:
        """Flush and close the underlying handler (important for file backend)."""
        self._handler.flush()
        self._handler.close()
        self._logger.removeHandler(self._handler)
