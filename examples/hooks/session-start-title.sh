#!/bin/bash
# Set GNU Screen window title to project directory name with lightning emoji.
# Hook type: SessionStart (sync)
# Input: JSON on stdin with .cwd field
set -uo pipefail

test -n "$STY" && screen -S "$STY" -p "$WINDOW" -X title "$(jq -r '.cwd | split("/") | last')"
