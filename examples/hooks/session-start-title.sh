#!/bin/bash
# Set terminal window title to ⚡<project> using ANSI escape sequences.
# Works inside bubblewrap sandbox — no screen socket access needed.
# Hook type: SessionStart (sync)
# Input: JSON on stdin with .cwd field
set -uo pipefail

PROJECT="$(jq -r '.cwd | split("/") | last')"

# \033k...\033\\ — Screen window title (interpreted by screen when $TERM=screen*)
# \033]2;...\007 — xterm/tmux window title
printf '\033k⚡%s\033\\' "$PROJECT" >&2
printf '\033]2;⚡%s\007' "$PROJECT" >&2
