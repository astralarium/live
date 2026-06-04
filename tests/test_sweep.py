"""Sweep verdict and SIGKILL-via-rm-f recovery."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from live.config import Config
from live.format import DEAD_NAME, INCONSISTENT_MARKER, LOCK_NAME, Meta, Watermarks
from live.session import SessionInfo, sweep_one
from live.verbose import emit_exit


def _stub_session(sessions_dir: Path, *, sid: str = "00000000-0000-4000-8000-000000000000") -> Path:
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


def test_sweep_negative_ttl_never_deletes(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(mode=0o700)
    sess = _stub_session(sessions_dir)
    (sess / LOCK_NAME).write_text("99999\n")
    (sess / "stream.0000.log").write_bytes(b"a\n")
    (sess / "lines.0000.idx").write_bytes(b"\x00" * 16)
    # Pre-stamp deadAt to a long-ago mtime so a positive TTL would delete it.
    dead = sess / DEAD_NAME
    dead.write_bytes(b"")
    long_ago = time.time() - 365 * 86400
    os.utime(dead, (long_ago, long_ago))

    sweep_one(sess, _cfg(ttl_days=-1))

    assert sess.exists()
    assert dead.exists()


# ----- exit-status helper -----


def _mk_info(status: str, exit_code: int | None) -> SessionInfo:
    return SessionInfo(
        id="x",
        path=Path("/x"),
        meta=Meta(id="x", command=["sh"], cwd="/", started_at=0.0),
        status=status,
        watermarks=Watermarks(0, 0, 0, 0, 0),
        last_activity=0.0,
        exited_at=None,
        exit_code=exit_code,
    )


def test_emit_exit_inconsistent_with_known_exit_code(capsys) -> None:
    """Race: recorder wrote meta with exit_code before crashing; sweeper later
    stamped deadAt inconsistent. Both lines should surface."""
    emit_exit(_mk_info("inconsistent", exit_code=42))
    err = capsys.readouterr().err
    assert "live: exit=inconsistent" in err
    assert "live: exit-code=42" in err
    # exit=inconsistent precedes exit-code so agents see the warning first.
    assert err.index("exit=inconsistent") < err.index("exit-code=42")


def test_emit_exit_exited(capsys) -> None:
    emit_exit(_mk_info("exited", exit_code=0))
    err = capsys.readouterr().err
    assert err == "live: exit-code=0\n"


def test_emit_exit_inconsistent_unknown_exit_code(capsys) -> None:
    emit_exit(_mk_info("inconsistent", exit_code=None))
    err = capsys.readouterr().err
    assert err == "live: exit=inconsistent\n"


def test_emit_exit_running_is_silent(capsys) -> None:
    emit_exit(_mk_info("running", exit_code=None))
    assert capsys.readouterr().err == ""


# ----- rm -f recovery -----


def test_rm_f_terminates_running_recorder(
    project: Path, run_live, spawn_run, wait_for
) -> None:
    proc = spawn_run("-n", "longrun")
    sessions = project / ".live" / "sessions"
    assert wait_for(lambda: sessions.exists() and any(sessions.iterdir()))
    [sess] = list(sessions.iterdir())
    assert wait_for(lambda: (sess / "meta.json").exists())

    rm = run_live(project, "rm", "-f", "longrun")
    assert rm.returncode == 0
    # Recorder process should die shortly after SIGTERM + dir unlink.
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        raise AssertionError("rm -f did not terminate the recorder")
    assert not sess.exists()
