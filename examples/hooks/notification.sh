#!/bin/bash
# Forward Claude Code notification via XMPP bridge.
# Hook type: Notification (async)
# Input: JSON on stdin with .session_id, .cwd, .notification_type, .title, .message fields
# Switch: ~/.config/xmpp-notify/notify-enabled
#
# Uses "notify" command so the bridge can prepend the session icon + window ID
# (e.g. ⚡[~/project #2]). Falls back to plain send when bridge is unavailable.
set -uo pipefail

[ -f "$HOME/.config/xmpp-notify/notify-enabled" ] || exit 0

INPUT="$(cat)"
TYPE="$(echo "$INPUT" | jq -r '.notification_type')"
MSG="$(echo "$INPUT" | jq -r '(.title // "") + ": " + .message')"

claude-xmpp-client notify "$(echo "$INPUT" | jq -c \
    --arg type "$TYPE" --arg msg "$MSG" \
    '{session_id: .session_id, message: ("[" + $type + "] " + $msg)}')"
