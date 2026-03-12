"""Integration smoke tests for Screen title-management primitives.

These tests do not run the OpenCode plugin itself; instead they exercise the
real GNU Screen commands that the plugin relies on under an intentionally noisy
configuration (caption + hardstatus + backtick refresh + altscreen).

The goal is to keep the low-level assumptions honest:

- ``screen -X title`` works in an isolated Screen session with aggressive UI
  settings.
- ``screen -X dynamictitle off/on`` is accepted by Screen and can be used by the
  plugin without depending on the user's global ``~/.screenrc``.
- ``screen -Q title`` reports the last explicit title, so future regressions in
  the plugin can be debugged against a known-good Screen baseline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


def _screen_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SCREENDIR"] = str(tmp_path / "screendir")
    screen_dir = Path(env["SCREENDIR"])
    screen_dir.mkdir()
    screen_dir.chmod(0o700)
    return env


def _screen_rc(tmp_path: Path) -> Path:
    rc = tmp_path / "screenrc"
    rc.write_text(
        "startup_message off\n"
        "defutf8 on\n"
        "utf8 on\n"
        "hardstatus alwayslastline \"%H %h %f %t %= %D %M %d.%m.%Y\"\n"
        "caption always \"%-w%{= BW}%50>%n %t%{-}%+w%<\"\n"
        "backtick 1 1 1 date +%T\n"
        "shelltitle \"$ |bash\"\n"
        "altscreen on\n"
    )
    return rc


def _run_screen(env: dict[str, str], *args: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["screen", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _query_title(env: dict[str, str], session: str, attempts: int = 5) -> subprocess.CompletedProcess[str]:
    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(attempts):
        last = _run_screen(env, "-S", session, "-p", "0", "-Q", "title")
        if last.returncode == 0:
            return last
        stdout = last.stdout or ""
        if "-queryA" in stdout and "Address already in use" in stdout:
            screen_dir = Path(env["SCREENDIR"])
            for stale in screen_dir.glob("*-queryA"):
                stale.unlink(missing_ok=True)
        time.sleep(0.5)
    assert last is not None
    combined = f"{last.stdout}\n{last.stderr}"
    if "Address already in use" in combined or "chown: No such file or directory" in combined:
        pytest.skip(f"screen -Q title unreliable in this environment: {combined.strip()}")
    return last


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("screen") is None, reason="screen not installed")
class TestScreenTitleIntegration:
    """Smoke-test real Screen title commands used by the plugin."""

    def test_title_and_dynamictitle_commands_work_with_aggressive_screenrc(self, tmp_path: Path):
        env = _screen_env(tmp_path)
        rc = _screen_rc(tmp_path)
        session = f"title-int-{os.getpid()}"

        start = _run_screen(env, "-d", "-m", "-S", session, "-c", str(rc), "bash", "--noprofile", "--norc")
        assert start.returncode == 0, start.stderr

        try:
            time.sleep(0.5)

            res = _run_screen(env, "-S", session, "-p", "0", "-X", "title", "INITIAL")
            assert res.returncode == 0, res.stderr

            res = _query_title(env, session)
            assert res.returncode == 0, res.stderr
            assert res.stdout.strip() == "INITIAL"

            res = _run_screen(env, "-S", session, "-p", "0", "-X", "dynamictitle", "off")
            assert res.returncode == 0, res.stderr

            res = _run_screen(env, "-S", session, "-p", "0", "-X", "hstatus", "")
            assert res.returncode == 0, res.stderr

            res = _run_screen(env, "-S", session, "-p", "0", "-X", "title", "AFTER_OFF")
            assert res.returncode == 0, res.stderr

            res = _query_title(env, session)
            assert res.returncode == 0, res.stderr
            assert res.stdout.strip() == "AFTER_OFF"

            res = _run_screen(env, "-S", session, "-p", "0", "-X", "dynamictitle", "on")
            assert res.returncode == 0, res.stderr

            res = _run_screen(env, "-S", session, "-p", "0", "-X", "title", "FINAL")
            assert res.returncode == 0, res.stderr

            res = _query_title(env, session)
            assert res.returncode == 0, res.stderr
            assert res.stdout.strip() == "FINAL"
        finally:
            _run_screen(env, "-S", session, "-X", "quit")
