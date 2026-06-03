"""Partial-line tail surfacing via tail --since-line."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_for_session(project: Path) -> Path:
    sessions = project / ".live" / "sessions"
    assert _wait_for(lambda: any(sessions.iterdir()) if sessions.exists() else False)
    [d] = list(sessions.iterdir())
    return d


def test_partial_line_surfaces_in_tail(project: Path, live_env, run_live) -> None:
    # Launch a recorder that emits one complete line, a partial prompt, then sleeps.
    # The trailing prompt has NO newline -> should appear as a partial line.
    script = (
        "printf 'first complete line\\n'; "
        "printf 'Continue? [Y/n] '; "
        "sleep 10"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "prompt", "--",
         "sh", "-c", script],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sess_dir = _wait_for_session(project)
        # Wait until at least one complete line is recorded AND a partial tail exists.
        stream = sess_dir / "stream.0000.log"
        idx = sess_dir / "lines.0000.idx"

        def has_partial() -> bool:
            try:
                s = stream.read_bytes()
                i = idx.read_bytes()
            except FileNotFoundError:
                return False
            # One indexed line (16 bytes) AND stream contains the partial prompt.
            return len(i) == 16 and b"Continue?" in s and not s.endswith(b"\n")

        assert _wait_for(has_partial, timeout=8.0), "partial state never appeared"

        poll = run_live(project, "tail", "--since-line", "0", "prompt")
        # stdout should include both the indexed line and the partial prompt bytes.
        out = poll.stdout.replace("\r", "")
        assert "first complete line" in out
        assert "Continue? [Y/n] " in out
        # stderr should announce the partial-line stderr signal.
        assert "live: partial-line bytes=" in poll.stderr
        assert "age=" in poll.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
