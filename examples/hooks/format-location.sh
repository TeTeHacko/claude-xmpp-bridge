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
    # Use pure bash parameter substitution instead of sed to avoid injection
    # via special characters in $HOME or $REG_PROJECT (e.g. |, \, &).
    case "$REG_PROJECT" in
        "$HOME"/*)  SHORT_PROJECT="~/${REG_PROJECT#"$HOME"/}" ;;
        "$HOME")    SHORT_PROJECT="~" ;;
        *)          SHORT_PROJECT="$REG_PROJECT" ;;
    esac
    if [ "$SHORT_PROJECT" = "$CWD" ]; then
        echo "$CWD"
    else
        echo "${SHORT_PROJECT} → ${CWD}"
    fi
else
    echo "$CWD"
fi
