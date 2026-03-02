#!/bin/bash
# Forward Claude Code notification via XMPP bridge.
# Hook type: Notification (async)
# Input: JSON on stdin with .session_id, .cwd, .notification_type, .title, .message fields
# Switch: ~/.config/xmpp-notify/notify-enabled
set -uo pipefail

[ -f "$HOME/.config/xmpp-notify/notify-enabled" ] || exit 0

INPUT="$(cat)"
LOC="$(echo "$INPUT" | ~/.claude/hooks/format-location.sh)"
TYPE="$(echo "$INPUT" | jq -r '.notification_type')"
MSG="$(echo "$INPUT" | jq -r '(.title // "") + ": " + .message')"

echo "[${LOC}] [${TYPE}] ${MSG}" | claude-xmpp-client send
