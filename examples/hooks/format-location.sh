#!/bin/bash
# Format session location: "[project → cwd]" or "[cwd]" if same.
# Usage: echo "$HOOK_JSON" | format-location.sh
# Outputs the location string (without brackets).
set -uo pipefail

INPUT="$(cat)"
SID="$(echo "$INPUT" | jq -r '.session_id')"
CWD="$(echo "$INPUT" | jq -r '.cwd | if . == env.HOME then "~" elif startswith(env.HOME + "/") then "~" + ltrimstr(env.HOME) else . end')"

REG_PROJECT="$(claude-xmpp-client query "$SID" 2>/dev/null)" || REG_PROJECT=""
if [ -n "$REG_PROJECT" ]; then
    SHORT_PROJECT="$(echo "$REG_PROJECT" | sed "s|^$HOME/|~/|; s|^$HOME$|~|")"
    if [ "$SHORT_PROJECT" = "$CWD" ]; then
        echo "$CWD"
    else
        echo "${SHORT_PROJECT} → ${CWD}"
    fi
else
    echo "$CWD"
fi
