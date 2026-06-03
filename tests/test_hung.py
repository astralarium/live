"""Hung-status surfacing via backdating the active idx mtime."""

from __future__ import annotations

import json
import os
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
    assert _wait_for(lambda: sessions.exists() and any(sessions.iterdir()))
    [d] = list(sessions.iterdir())
    return d


def test_ls_reports_hung_when_idx_mtime_is_stale(
    project: Path, live_env, run_live
) -> None:
    # Configure heartbeat to 1s so the hung threshold is 3s.
    (project / ".live").mkdir(mode=0o700, exist_ok=True)
    (project / ".live" / "config.json").write_text(
        json.dumps({"heartbeatSec": 1})
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "longrun", "--",
         "sh", "-c", "echo started; sleep 60"],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sess_dir = _wait_for_session(project)
        idx = sess_dir / "lines.0000.idx"
        assert _wait_for(lambda: idx.exists() and idx.stat().st_size > 0,
                         timeout=8.0), "no indexed line ever appeared"
        # Backdate the active idx by 10s -> well past 3 * heartbeatSec.
        past = time.time() - 10
        os.utime(idx, (past, past))

        ls = run_live(project, "ls", "--json")
        info = json.loads(ls.stdout.strip().splitlines()[0])
        assert info["status"] == "hung"

        # tail -v should also surface "status=hung last-activity=<s>".
        tail = run_live(project, "tail", "--since-line", "0", "longrun")
        assert "status=hung last-activity=" in tail.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
