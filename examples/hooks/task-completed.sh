#!/bin/bash
# Send task completion notification via XMPP bridge.
# Hook type: TaskCompleted (async)
# Input: JSON on stdin with .session_id, .cwd, .task_subject fields
# Switch: ~/.config/xmpp-notify/notify-enabled
set -uo pipefail

[ -f "$HOME/.config/xmpp-notify/notify-enabled" ] || exit 0

INPUT="$(cat)"
LOC="$(echo "$INPUT" | ~/.claude/hooks/format-location.sh)"
SUBJ="$(echo "$INPUT" | jq -r '.task_subject')"

echo "[${LOC}] Task completed: ${SUBJ}" | claude-xmpp-client send
