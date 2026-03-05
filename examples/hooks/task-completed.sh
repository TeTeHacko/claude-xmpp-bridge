#!/bin/bash
# Send task completion notification via XMPP bridge.
# Hook type: TaskCompleted (async)
# Input: JSON on stdin with .session_id, .cwd, .task_subject fields
# Switch: ~/.config/xmpp-notify/notify-enabled
#
# Uses "notify" command so the bridge can prepend the session icon + window ID
# (e.g. ⚡[~/project #2]). Falls back to plain send when bridge is unavailable.
set -uo pipefail

[ -f "$HOME/.config/xmpp-notify/notify-enabled" ] || exit 0

INPUT="$(cat)"
SUBJ="$(echo "$INPUT" | jq -r '.task_subject')"

claude-xmpp-client notify "$(echo "$INPUT" | jq -c \
    --arg subj "$SUBJ" \
    '{session_id: .session_id, message: ("Task completed: " + $subj)}')"
