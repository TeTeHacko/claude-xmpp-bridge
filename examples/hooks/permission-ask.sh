#!/bin/bash
set -uo pipefail

# On/off switch: touch ~/.config/xmpp-notify/ask-enabled to enable
[ -f "$HOME/.config/xmpp-notify/ask-enabled" ] || exit 0

INPUT="$(cat)"

TOOL="$(echo "$INPUT" | jq -r '.tool_name')"
LOCATION="$(echo "$INPUT" | ~/.claude/hooks/format-location.sh)"

# Build human-readable description of what's being requested
case "$TOOL" in
    Bash)
        DESC="$(echo "$INPUT" | jq -r '.tool_input.description // empty')"
        CMD="$(echo "$INPUT" | jq -r '.tool_input.command | .[0:300]')"
        if [ -n "$DESC" ]; then
            DETAIL="${DESC}
$ ${CMD}"
        else
            DETAIL="$ ${CMD}"
        fi
        ;;
    Write)
        DETAIL="$(echo "$INPUT" | jq -r '.tool_input.file_path')"
        ;;
    Edit)
        FILE="$(echo "$INPUT" | jq -r '.tool_input.file_path')"
        OLD="$(echo "$INPUT" | jq -r '.tool_input.old_string | .[0:100]')"
        DETAIL="${FILE}
- ${OLD}..."
        ;;
    *)
        DETAIL="$(echo "$INPUT" | jq -r '.tool_input | tostring | .[0:200]')"
        ;;
esac

MSG="[${LOCATION}] ${TOOL}
${DETAIL}

Povolit? (y/n/a=always)"

REPLY="$(claude-xmpp-ask --timeout 300 "$MSG" 2>/dev/null)" || exit 0

case "$REPLY" in
    a|A|always|Always|ALWAYS|vzdy|Vzdy|VZDY)
        # Allow + apply "always allow" permission rule from suggestions
        SUGGESTIONS="$(echo "$INPUT" | jq -c '.permission_suggestions // []')"
        if [ "$SUGGESTIONS" != "[]" ] && [ "$SUGGESTIONS" != "null" ]; then
            echo "$INPUT" | jq '{
                hookSpecificOutput: {
                    hookEventName: "PermissionRequest",
                    decision: {
                        behavior: "allow",
                        updatedPermissions: .permission_suggestions
                    }
                }
            }'
        else
            jq -n '{
                hookSpecificOutput: {
                    hookEventName: "PermissionRequest",
                    decision: {
                        behavior: "allow"
                    }
                }
            }'
        fi
        ;;
    y|Y|yes|YES|ano|Ano|ANO|j|J|ja|jo|Jo|JO)
        jq -n '{
            hookSpecificOutput: {
                hookEventName: "PermissionRequest",
                decision: {
                    behavior: "allow"
                }
            }
        }'
        ;;
    *)
        jq -n --arg reason "$REPLY" '{
            hookSpecificOutput: {
                hookEventName: "PermissionRequest",
                decision: {
                    behavior: "deny",
                    message: ("Denied via XMPP: " + $reason)
                }
            }
        }'
        ;;
esac
