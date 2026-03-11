"""Tests for messages module — defaults, TOML loading, and format strings."""

from __future__ import annotations

import json
from dataclasses import fields

from claude_xmpp_bridge.messages import Messages, format_generated_agent_message, load_messages


class TestMessagesDefaults:
    """Messages() without arguments must provide all EN defaults."""

    def test_default_values_are_set(self):
        msgs = Messages()
        assert msgs.bridge_started == "XMPP Bridge started."
        assert msgs.bridge_stopped == "XMPP Bridge stopped."
        assert msgs.no_sessions == "No active sessions."
        assert msgs.session_list_header == "Sessions:"
        assert msgs.active_marker == "* = active session"
        assert msgs.sent == "sent"
        assert msgs.read_only_tag == "read-only"

    def test_all_fields_are_strings(self):
        msgs = Messages()
        for f in fields(Messages):
            assert isinstance(getattr(msgs, f.name), str), f"{f.name} is not a str"

    def test_help_text_contains_commands(self):
        msgs = Messages()
        assert "/list" in msgs.help_text
        assert "/help" in msgs.help_text


class TestLoadMessagesNone:
    """load_messages(None) must return unmodified defaults."""

    def test_returns_defaults(self):
        msgs = load_messages(None)
        default = Messages()
        for f in fields(Messages):
            assert getattr(msgs, f.name) == getattr(default, f.name)


class TestLoadMessagesNonexistentPath:
    """load_messages(nonexistent_path) must return defaults."""

    def test_missing_file_returns_defaults(self, tmp_path):
        missing = tmp_path / "does_not_exist.toml"
        msgs = load_messages(missing)
        default = Messages()
        for f in fields(Messages):
            assert getattr(msgs, f.name) == getattr(default, f.name)


class TestLoadMessagesFromToml:
    """Loading from a valid TOML file overrides specific keys."""

    def test_override_single_key(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('sent = "odesláno"\n')
        msgs = load_messages(toml_file)
        assert msgs.sent == "odesláno"

    def test_override_multiple_keys(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('bridge_started = "Most XMPP spuštěn."\nbridge_stopped = "Most XMPP zastaven."\n')
        msgs = load_messages(toml_file)
        assert msgs.bridge_started == "Most XMPP spuštěn."
        assert msgs.bridge_stopped == "Most XMPP zastaven."

    def test_non_overridden_keys_keep_defaults(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('sent = "odesláno"\n')
        msgs = load_messages(toml_file)
        default = Messages()
        assert msgs.bridge_started == default.bridge_started
        assert msgs.help_text == default.help_text


class TestUnknownKeysIgnored:
    """Unknown keys present in TOML must not raise or leak onto the object."""

    def test_unknown_key_is_ignored(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('bogus_key = "should be ignored"\n')
        msgs = load_messages(toml_file)
        assert not hasattr(msgs, "bogus_key")

    def test_known_and_unknown_together(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('sent = "odesláno"\ntotally_unknown = "nope"\n')
        msgs = load_messages(toml_file)
        assert msgs.sent == "odesláno"
        assert not hasattr(msgs, "totally_unknown")

    def test_non_string_value_is_ignored(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text("sent = 42\n")
        msgs = load_messages(toml_file)
        default = Messages()
        assert msgs.sent == default.sent


class TestMissingKeysFallback:
    """A TOML file with only some keys must leave the rest at defaults."""

    def test_partial_override(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('no_sessions = "Žádné relace."\n')
        msgs = load_messages(toml_file)
        default = Messages()
        assert msgs.no_sessions == "Žádné relace."
        # Every other field must match the default.
        for f in fields(Messages):
            if f.name == "no_sessions":
                continue
            assert getattr(msgs, f.name) == getattr(default, f.name), f"{f.name} should be default"

    def test_empty_toml_returns_all_defaults(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text("")
        msgs = load_messages(toml_file)
        default = Messages()
        for f in fields(Messages):
            assert getattr(msgs, f.name) == getattr(default, f.name)


class TestFormatStrings:
    """Messages containing {placeholders} must work with str.format()."""

    def test_delivery_failed_format(self):
        msgs = Messages()
        result = msgs.delivery_failed.format(project="test")
        assert result == "Delivery to [test] failed"

    def test_no_backend_format(self):
        msgs = Messages()
        result = msgs.no_backend.format(project="my-project")
        assert result == "Session [my-project] has no multiplexer — cannot deliver message"

    def test_session_not_found_format(self):
        msgs = Messages()
        result = msgs.session_not_found.format(index=3)
        assert result == "Session #3 not found. Type /list."

    def test_unknown_command_format(self):
        msgs = Messages()
        result = msgs.unknown_command.format(cmd="/foo")
        assert result == "Unknown command: /foo\nType /help for help."

    def test_usage_send_to_format(self):
        msgs = Messages()
        result = msgs.usage_send_to.format(cmd="/5")
        assert result == "Usage: /5 <message>"

    def test_overridden_format_string(self, tmp_path):
        toml_file = tmp_path / "messages.toml"
        toml_file.write_text('delivery_failed = "Doručení do [{project}] selhalo"\n')
        msgs = load_messages(toml_file)
        result = msgs.delivery_failed.format(project="test")
        assert result == "Doručení do [test] selhalo"


class TestGeneratedAgentMessage:
    def test_format_generated_agent_message_has_marker_and_json_meta(self):
        wrapped = format_generated_agent_message(
            msg_type="relay",
            message="hello from window 2",
            from_session_id="ses_A",
            to_session_id="ses_B",
            mode="screen",
            message_id="abc123def456",
        )

        lines = wrapped.splitlines()
        assert lines[0] == "[bridge-generated message]"
        meta = json.loads(lines[1])
        assert meta["type"] == "relay"
        assert meta["generated"] is True
        assert meta["from"] == "ses_A"
        assert meta["to"] == "ses_B"
        assert meta["mode"] == "screen"
        assert meta["message_id"] == "abc123def456"
        assert wrapped.endswith("hello from window 2")

    def test_format_generated_agent_message_supports_null_fields(self):
        wrapped = format_generated_agent_message(msg_type="broadcast", message="hello all")
        meta = json.loads(wrapped.splitlines()[1])
        assert meta["type"] == "broadcast"
        assert meta["from"] is None
        assert meta["to"] is None
        assert meta["mode"] is None
        assert meta["message_id"] is None

    def test_format_generated_agent_message_does_not_double_wrap(self):
        once = format_generated_agent_message(msg_type="relay", message="hello")
        twice = format_generated_agent_message(msg_type="relay", message=once)
        assert twice == once
        assert twice.count("[bridge-generated message]") == 1
