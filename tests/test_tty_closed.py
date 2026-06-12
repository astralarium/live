"""TTY EOF with a live child: `(tty closed)` state and `[detached]` exits.

Platform note: Linux masters raise EIO once the last slave fd closes, so the
recorder sees EOF while the child lives and stamps `ttyClosedAt`. BSD/macOS
masters only EOF when the session leader exits, so the recorder-side marker
is exercised end-to-end on Linux only; the ps/tail surfaces are tested
cross-platform by planting the marker.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from live.format import read_meta, write_meta_atomic

# Releases every slave fd (PTY EOF on Linux) while the shell keeps running.
_CLOSE_TTY = "exec >/dev/null 2>&1 0</dev/null"


def _info(project: Path, run_live, selector: str) -> dict:
    out = run_live(project, "ps", "-ag", "--json", selector)
    rows = out.stdout.splitlines()
    return json.loads(rows[0]) if rows else {}


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="BSD/macOS PTY masters only EOF on session-leader exit",
)
def test_tty_closed_session_lifecycle(
    project: Path, live_env, run_live, wait_for
) -> None:
    rec = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "live.cli",
            "run",
            "-n",
            "ttyc",
            "--",
            "sh",
            "-c",
            f"echo before; {_CLOSE_TTY}; sleep 5",
        ],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Recorder marks the session once the PTY hits EOF with a live child.
        assert wait_for(
            lambda: _info(project, run_live, "ttyc").get("ttyClosedAt"),
            timeout=8.0,
        ), "ttyClosedAt never appeared"

        info = _info(project, run_live, "ttyc")
        assert info["status"] == "running"  # heartbeats continue; not hung
        human = run_live(project, "ps", "ttyc").stdout
        assert "(tty closed)" in human

        # tail -f drains and exits instead of waiting on a dry stream.
        t0 = time.time()
        f = run_live(project, "tail", "-fv", "ttyc", timeout=10)
        assert "before" in f.stdout.replace("\r", "")
        assert "tty closed; no further output" in f.stderr
        assert time.time() - t0 < 5

        # Foreground recorder explains the wait, then exits with the child.
        _, err = rec.communicate(timeout=15)
        assert rec.returncode == 0
        assert "child closed its terminal" in err

        info = _info(project, run_live, "ttyc")
        assert info["status"] == "exited"
        assert info["exitCode"] == 0
        assert "detached" not in info  # no survivors in the child's group
    finally:
        if rec.poll() is None:
            rec.kill()
            rec.wait(timeout=5)


def test_tty_closed_marker_surfaces_in_ps_and_ends_follow(
    project: Path, live_env, run_live, wait_for, spawn_run, wait_for_session
) -> None:
    # Cross-platform: plant the marker the recorder writes on Linux and
    # assert the reader-side surfaces (ps suffix, JSON field, tail -f exit).
    spawn_run("-n", "marked")
    sess = wait_for_session()
    assert wait_for(lambda: read_meta(sess) is not None)
    meta = read_meta(sess)
    write_meta_atomic(sess, replace(meta, tty_closed_at=time.time()))

    info = _info(project, run_live, "marked")
    assert info["status"] == "running"
    assert info["ttyClosedAt"] > 0
    human = run_live(project, "ps", "marked").stdout
    assert "(tty closed)" in human

    t0 = time.time()
    f = run_live(project, "tail", "-fv", "marked", timeout=10)
    assert "go" in f.stdout.replace("\r", "")  # drained before exiting
    assert "tty closed; no further output" in f.stderr
    assert "next-line=" in f.stderr
    assert "exit-code=" not in f.stderr  # session is still running
    assert time.time() - t0 < 5

    # Non-verbose follow exits silently.
    quiet = run_live(project, "tail", "-f", "marked", timeout=10)
    assert quiet.stderr == ""


def test_background_survivor_marks_detached(project: Path, run_live) -> None:
    # The backgrounded sleep redirects its stdio so the PTY hits EOF as soon
    # as the shell (session leader) exits, and stays in the child's process
    # group. The HUP shield must be inherited at fork time (`trap` in the
    # parent shell): the kernel's leader-exit SIGHUP races a `nohup` exec
    # and would intermittently kill the survivor before it shields itself.
    run_live(
        project,
        "run",
        "-n",
        "det",
        "--",
        "sh",
        "-c",
        'trap "" HUP; sleep 5 >/dev/null 2>&1 </dev/null & echo started',
    )
    info = _info(project, run_live, "det")
    assert info["status"] == "exited"
    assert info.get("detached") is True
    human = run_live(project, "ps", "-a", "det").stdout
    assert "[detached]" in human


def test_clean_exit_is_not_detached(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "plain", "--", "sh", "-c", "echo done")
    info = _info(project, run_live, "plain")
    assert info["status"] == "exited"
    assert "detached" not in info
    assert "ttyClosedAt" not in info
    human = run_live(project, "ps", "-a", "plain").stdout
    assert "[detached]" not in human
    assert "(tty closed)" not in human
