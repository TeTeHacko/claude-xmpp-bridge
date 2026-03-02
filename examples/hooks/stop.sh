#!/bin/bash
# Send last assistant message via XMPP bridge when Claude session stops.
# Hook type: Stop (async)
# Input: JSON on stdin with .session_id, .cwd, .last_assistant_message fields
# Switch: ~/.config/xmpp-notify/notify-enabled
set -uo pipefail

[ -f "$HOME/.config/xmpp-notify/notify-enabled" ] || exit 0

INPUT="$(cat)"
claude-xmpp-client response "$(echo "$INPUT" | jq -c \
    '{session_id: .session_id, project: .cwd, message: (.last_assistant_message | .[0:500] // "done")}')"
