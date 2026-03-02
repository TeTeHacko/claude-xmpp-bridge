#!/bin/bash
# Unregister Claude session from the XMPP bridge daemon.
# Hook type: SessionEnd (async)
# Input: JSON on stdin with .session_id field
set -uo pipefail

SID="$(jq -r '.session_id')"
claude-xmpp-client unregister "$SID"
