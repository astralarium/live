"""Hung-status surfacing and heartbeat liveness signal."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path


def _write_config(project: Path, **fields) -> None:
    (project / ".live").mkdir(mode=0o700, exist_ok=True)
    (project / ".live" / "config.json").write_text(json.dumps(fields))


def test_ls_reports_hung_when_idx_mtime_is_stale(
    project: Path, run_live, spawn_run, wait_for, wait_for_session
) -> None:
    # Configure heartbeat to 1s so the hung threshold is 3s.
    _write_config(project, heartbeatSec=1)
    proc = spawn_run("-n", "longrun")
    sess_dir = wait_for_session()
    idx = sess_dir / "lines.0000.idx"
    assert wait_for(lambda: idx.exists() and idx.stat().st_size > 0, timeout=8.0), (
        "no indexed line ever appeared"
    )
    # Freeze the recorder so no heartbeat or trailing startup write can
    # re-touch the idx after the backdate. A stopped process still holds the
    # lock, which is exactly the "hung" shape: alive but silent.
    os.kill(proc.pid, signal.SIGSTOP)
    try:
        # Backdate the active idx by 10s -> well past 3 * heartbeatSec.
        # SIGSTOP delivery is asynchronous, so a write already in flight can
        # land just after the first utime; re-apply until it sticks.
        past = time.time() - 10

        def _backdate() -> bool:
            os.utime(idx, (past, past))
            return idx.stat().st_mtime < past + 1

        assert wait_for(_backdate, timeout=5.0), "idx mtime kept advancing"

        ls = run_live(project, "ls", "--json")
        info = json.loads(ls.stdout.strip().splitlines()[0])
        assert info["status"] == "hung"

        # tail -v should also surface "status=hung last-activity=<s>".
        tail = run_live(project, "tail", "-vn", "+0", "longrun")
        assert "status=hung last-activity=" in tail.stderr
    finally:
        # Resume so fixture teardown can SIGTERM it normally.
        os.kill(proc.pid, signal.SIGCONT)


def test_heartbeat_advances_idx_mtime_while_silent(
    project: Path, spawn_run, wait_for, wait_for_session
) -> None:
    """The recorder touches the active idx mtime every heartbeatSec even while
    no bytes flow. With heartbeatSec=1, poll until the mtime advances; the wide
    deadline tolerates a loaded machine starving the recorder loop."""
    _write_config(project, heartbeatSec=1)
    spawn_run("-n", "beat")
    sess_dir = wait_for_session()
    idx = sess_dir / "lines.0000.idx"
    assert wait_for(lambda: idx.exists() and idx.stat().st_size >= 40, timeout=8.0), (
        "no indexed line ever appeared"
    )
    t0 = idx.stat().st_mtime
    assert wait_for(lambda: idx.stat().st_mtime > t0, timeout=8.0), (
        f"idx mtime did not advance during silence (stuck at {t0})"
    )
