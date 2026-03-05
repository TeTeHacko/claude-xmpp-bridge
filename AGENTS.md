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
