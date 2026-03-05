"""Tests for the AuditLogger module."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_xmpp_bridge.audit import AuditLogger


class TestAuditLoggerFile:
    """AuditLogger with a file backend."""

    def test_creates_file_and_writes_jsonl(self, tmp_path: Path) -> None:
        """Writing an event should create the file and produce a JSON line."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        al.log("TEST_EVENT")
        al.close()

        assert log_file.exists()
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "TEST_EVENT"

    def test_record_has_ts_and_event(self, tmp_path: Path) -> None:
        """Every record must have 'ts' (ISO 8601 UTC) and 'event' fields."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        al.log("TS_CHECK")
        al.close()

        record = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert "ts" in record
        assert record["ts"].endswith("Z")
        assert "T" in record["ts"]
        assert record["event"] == "TS_CHECK"

    def test_record_preserves_unicode(self, tmp_path: Path) -> None:
        """Unicode data must not be escaped — ensure_ascii=False."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        al.log("UNICODE_TEST", body="héllo wörld — привет 🌍")
        al.close()

        raw = log_file.read_text(encoding="utf-8").strip()
        # Characters must appear verbatim, not as \uXXXX escapes
        assert "héllo wörld" in raw
        assert "привет" in raw
        assert "🌍" in raw
        record = json.loads(raw)
        assert record["body"] == "héllo wörld — привет 🌍"

    def test_record_includes_all_data_fields(self, tmp_path: Path) -> None:
        """All kwargs passed to log() must appear in the JSON record."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        al.log("FULL_RECORD", from_jid="user@example.com", allowed=True, body_len=42)
        al.close()

        record = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert record["from_jid"] == "user@example.com"
        assert record["allowed"] is True
        assert record["body_len"] == 42

    def test_close_closes_handler(self, tmp_path: Path) -> None:
        """close() must flush and close the underlying handler."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        al.log("CLOSE_TEST")
        al.close()

        # After close the handler should be detached from the logger
        logger = logging.getLogger("claude_xmpp_bridge.audit")
        assert al._handler not in logger.handlers


class TestAuditLoggerJournald:
    """AuditLogger journald / syslog / stderr fallback chain."""

    def test_uses_journalhandler_if_available(self, tmp_path: Path) -> None:
        """When systemd.journal is importable, JournalHandler must be used."""
        fake_journal_handler = MagicMock(spec=logging.Handler)
        fake_journal_handler.return_value = MagicMock(spec=logging.Handler)

        fake_module = MagicMock()
        fake_module.JournalHandler = fake_journal_handler

        with patch.dict("sys.modules", {"systemd": MagicMock(), "systemd.journal": fake_module}):
            al = AuditLogger("journald")
            al.close()

        fake_journal_handler.assert_called_once()

    def test_falls_back_to_syslog_if_no_systemd(self) -> None:
        """Without systemd, must fall back to SysLogHandler."""
        with (
            patch.dict("sys.modules", {"systemd": None, "systemd.journal": None}),
            patch("logging.handlers.SysLogHandler") as MockSysLog,
        ):
            MockSysLog.return_value = MagicMock(spec=logging.Handler)
            al = AuditLogger("journald")
            al.close()

        MockSysLog.assert_called_once()

    def test_falls_back_to_stderr_if_no_syslog(self) -> None:
        """Without systemd and without /dev/log, must fall back to stderr."""
        with (
            patch.dict("sys.modules", {"systemd": None, "systemd.journal": None}),
            patch("logging.handlers.SysLogHandler", side_effect=OSError("no /dev/log")),
        ):
            al = AuditLogger("journald")
            al.close()

        # If we reached here without exception, stderr handler was used
        assert al._handler is not None


class TestAuditLoggerIntegration:
    """Integration-level smoke tests."""

    def test_log_does_not_raise(self, tmp_path: Path) -> None:
        """log() must never raise, even for edge-case inputs."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        try:
            al.log("SMOKE")
            al.log("WITH_NONE", value=None)
            al.log("WITH_NESTED", data={"a": [1, 2, 3]})
            al.log("EMPTY_STR", s="")
        finally:
            al.close()

    def test_multiple_events_produce_multiple_lines(self, tmp_path: Path) -> None:
        """Each log() call must produce exactly one JSON line."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))
        events = ["EVT_A", "EVT_B", "EVT_C"]
        for ev in events:
            al.log(ev)
        al.close()

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == len(events)
        for line, expected_event in zip(lines, events, strict=True):
            record = json.loads(line)
            assert record["event"] == expected_event

    def test_serialization_error_emits_fallback(self, tmp_path: Path) -> None:
        """When a value is not JSON-serialisable, a fallback record is emitted instead."""
        log_file = tmp_path / "audit.log"
        al = AuditLogger(str(log_file))

        class _Unserializable:
            pass

        al.log("BAD_DATA", obj=_Unserializable())
        al.close()

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "BAD_DATA"
        assert "audit_serialization_error" in record
