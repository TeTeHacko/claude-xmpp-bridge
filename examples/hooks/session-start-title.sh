#!/bin/bash
# Set terminal window title to ⚡<project> using ANSI escape sequences.
# Works inside bubblewrap sandbox — no screen socket access needed.
# Hook type: SessionStart (sync)
# Input: JSON on stdin with .cwd field
set -uo pipefail

PROJECT="$(jq -r '.cwd | split("/") | last')"

# Prefer screen -X title (socket-based, no TUI interference).
# Falls back to ANSI escape sequences only when the socket is unavailable
# (bwrap sandbox: setsid() → no controlling terminal → socket inaccessible).
#
# Why avoid raw escape sequences outside sandbox:
#   Writing \033k...\033\\ directly to stderr (the inherited Screen pty) while
#   OpenCode TUI is active causes artefacts — Screen redraws caption/hardstatus
#   at the wrong moment, producing doubled window lists, flickering, and garbage.
if [[ -n "${STY:-}" ]] && screen -S "$STY" -p "${WINDOW:-0}" -X title "⚡$PROJECT" 2>/dev/null; then
  exit 0
fi

# Sandbox / no-Screen fallback: ANSI escape sequences via stderr (inherited pty fd).
# \033k...\033\\ — Screen window title (interpreted when $TERM=screen*)
# \033]2;...\007 — xterm/tmux window title
printf '\033k⚡%s\033\\' "$PROJECT" >&2
printf '\033]2;⚡%s\007' "$PROJECT" >&2
