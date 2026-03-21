#!/bin/bash
# Register Claude session with the XMPP bridge daemon.
# Hook type: SessionStart (async)
# Input: JSON on stdin with .session_id, .cwd fields
set -uo pipefail

INPUT="$(cat)"

if [ -n "${STY:-}" ]; then
	BACKEND=screen
	MUX_ID="$STY"
	MUX_WIN="${WINDOW:-0}"
elif [ -n "${TMUX:-}" ]; then
	BACKEND=tmux
	MUX_ID="${TMUX_PANE:-}"
	MUX_WIN=""
else
	BACKEND=none
	MUX_ID=""
	MUX_WIN=""
fi

claude-xmpp-client register "$(echo "$INPUT" | jq -c \
	--arg sty "$MUX_ID" \
	--arg win "$MUX_WIN" \
	--arg backend "$BACKEND" \
	--arg source "claude-code" \
	--arg pv "hook" \
	'{session_id: .session_id, sty: $sty, window: $win, project: .cwd, backend: $backend, source: $source, plugin_version: $pv}')"
