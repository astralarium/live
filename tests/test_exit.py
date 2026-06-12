"""Recorder exit-code propagation: 0, non-zero, command-not-found (127)."""

from __future__ import annotations

import json
from pathlib import Path


def _only_session(project: Path) -> dict:
    sessions = list((project / ".live" / "sessions").iterdir())
    assert len(sessions) == 1
    return json.loads((sessions[0] / "meta.json").read_text())


def test_run_propagates_nonzero_exit_code(project: Path, run_live) -> None:
    proc = run_live(
        project,
        "run",
        "-n",
        "x",
        "--",
        "sh",
        "-c",
        "exit 42",
        check=False,
    )
    assert proc.returncode == 42

    meta = _only_session(project)
    assert meta["exitCode"] == 42

    out = run_live(project, "tail", "-vn", "+0", "x")
    assert "exit-code=42" in out.stderr


def test_run_command_not_found_exits_127(project: Path, run_live) -> None:
    """`pty.fork`'d child can't `execvp` -> os._exit(127); parent reaps and
    propagates the status."""
    proc = run_live(
        project,
        "run",
        "--",
        "definitely-not-a-real-cmd-xyz-12345",
        check=False,
    )
    assert proc.returncode == 127
    # The child's error message is written to fd 2 in the PTY, which the parent
    # mirrors to its own stdout (slave PTY merges both streams).
    assert "command not found" in (proc.stdout + proc.stderr)

    meta = _only_session(project)
    assert meta["exitCode"] == 127


def test_run_propagates_zero_exit_code(project: Path, run_live) -> None:
    proc = run_live(project, "run", "-n", "ok", "--", "sh", "-c", "true")
    assert proc.returncode == 0
    meta = _only_session(project)
    assert meta["exitCode"] == 0
