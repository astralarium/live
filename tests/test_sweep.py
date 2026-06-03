"""Sweep verdict and SIGKILL-via-rm-f recovery."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from live.config import Config
from live.format import DEAD_NAME, INCONSISTENT_MARKER, LOCK_NAME
from live.sweep import sweep_one


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _stub_session(sessions_dir: Path, *, sid: str = "0190fake-0000-7000-8000-000000000000") -> Path:
    d = sessions_dir / sid
    d.mkdir(mode=0o700, parents=True, exist_ok=False)
    return d


# ----- sweep verdict -----


def _cfg(**kw) -> Config:
    defaults = dict(ttl_days=7, max_kb=512, segment_kb=64, heartbeat_sec=30)
    defaults.update(kw)
    return Config(**defaults)


def test_sweep_stamps_consistent_when_stream_and_idx_match(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(mode=0o700)
    sess = _stub_session(sessions_dir)
    # No lock file -> sweep skips the startup check; create dead lock.
    (sess / LOCK_NAME).write_text("99999\n")  # pid; flock probe will succeed.
    # 3 complete lines, 3 idx records -> consistent.
    (sess / "stream.0000.log").write_bytes(b"a\nb\nc\n")
    (sess / "lines.0000.idx").write_bytes(b"\x00" * 48)

    sweep_one(sess, _cfg())

    dead = sess / DEAD_NAME
    assert dead.exists()
    assert dead.read_bytes() == b""  # empty file = consistent


def test_sweep_stamps_inconsistent_when_stream_is_one_line_ahead(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(mode=0o700)
    sess = _stub_session(sessions_dir)
    (sess / LOCK_NAME).write_text("99999\n")
    # 3 complete lines but only 2 idx records -> crash mid-write.
    (sess / "stream.0000.log").write_bytes(b"a\nb\nc\n")
    (sess / "lines.0000.idx").write_bytes(b"\x00" * 32)

    sweep_one(sess, _cfg())

    dead = sess / DEAD_NAME
    assert dead.exists()
    assert dead.read_bytes() == INCONSISTENT_MARKER


def test_sweep_skips_session_without_lock_file(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(mode=0o700)
    sess = _stub_session(sessions_dir)
    # No process.lock at all -> sweep treats as starting and leaves it alone.
    sweep_one(sess, _cfg())
    assert not (sess / DEAD_NAME).exists()


# ----- rm -f recovery -----


def test_rm_f_terminates_running_recorder(project: Path, live_env, run_live) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "longrun", "--",
         "sh", "-c", "echo started; sleep 60"],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sessions = project / ".live" / "sessions"
        assert _wait_for(lambda: sessions.exists() and any(sessions.iterdir()))
        # Wait until the recorder has flock + meta written.
        [sess] = list(sessions.iterdir())
        assert _wait_for(lambda: (sess / "meta.json").exists())

        rm = run_live(project, "rm", "-f", "longrun")
        assert rm.returncode == 0
        # Recorder process should die shortly after SIGTERM + dir unlink.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            assert False, "rm -f did not terminate the recorder"
        # Session dir is gone.
        assert not sess.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
