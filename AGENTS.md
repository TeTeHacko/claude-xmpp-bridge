# AI Agent Instructions

When working in this repository, you MUST follow these rules:

1. **Tests & Linting**: Before committing ANY changes, you must ensure that all tests pass and static checks are clean.
   Run: `pytest`
   Run: `ruff check src/ tests/`
   Run: `mypy src/`

2. **Version Bump**: If you make any functional changes, add new features, or fix bugs, you MUST bump the version number before committing.
   Update the version in both `pyproject.toml` and `src/claude_xmpp_bridge/__init__.py`.

3. **Security**: Maintain the security posture.
   - Never use `shell=True` or raw `subprocess.Popen` when interacting with terminal multiplexers. Always use `asyncio.create_subprocess_exec` with a sanitized environment.
   - All Unix socket files must explicitly have `0600` permissions.
   - Token/credentials files must be checked for `0600` permissions before reading (use `_check_permissions()` from `config.py`).

4. **Documentation**: After any functional change, update the documentation to match:
   - `README.md` — keep configuration examples, CLI flags, and security notes accurate.
   - `CHANGELOG.md` — add an entry for the new version with a short description of changes.
   - `examples/opencode/plugins/xmpp-bridge.js` — keep `PLUGIN_VERSION` in sync with the package version in `pyproject.toml`.
   - `examples/opencode/README.md` and `examples/hooks/README.md` — keep behaviour descriptions and version references accurate.
   - If you add or remove config keys, CLI flags, environment variables, MCP tools, or socket commands, update all relevant sections in `README.md`.

5. **Inter-agent XMPP notifications**: Relay and broadcast XMPP observer messages use structured JSON (since v0.7.30).
   Tests must parse `json.loads(sent)` and assert on individual fields — never assert plain-text substrings.

   **Relay format** (socket `relay` command and MCP `send_message` tool):
   ```json
   {"type": "relay", "mode": "nudge|screen|inbox", "from": "sender_session_id|null", "to": "target_session_id", "message": "full text", "ts": 1741612800.123}
   ```
   MCP server adds `"message_id": "hex12chars"`.

   **Broadcast format** (socket `broadcast` command and MCP `broadcast_message` tool):
   ```json
   {"type": "broadcast", "mode": "nudge|screen", "from": "sender_session_id|null", "to": ["sid1", "sid2"], "message": "full text", "ts": 1741612800.123}
   ```

   All other XMPP messages (notify, ask, response, system) remain plain text.
