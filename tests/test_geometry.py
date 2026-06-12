"""PTY window size: 80x24 fallback without a terminal, `--geometry` override."""

from __future__ import annotations

import json
from pathlib import Path


def _wait_exited(project: Path, run_live, wait_for, sid: str) -> None:
    def _status() -> str | None:
        out = run_live(project, "ps", "-a", "--json").stdout
        for ln in out.splitlines():
            e = json.loads(ln)
            if e["id"] == sid:
                return e["status"]
        return None

    assert wait_for(lambda: _status() == "exited")


def test_detached_pty_defaults_to_80x24(project: Path, run_live, wait_for) -> None:
    res = run_live(project, "run", "-d", "--", "sh", "-c", "stty size")
    sid = res.stdout.strip()
    _wait_exited(project, run_live, wait_for, sid)
    assert run_live(project, "cat", sid[:8]).stdout.split() == ["24", "80"]


def test_geometry_flag_sets_pty_size(project: Path, run_live, wait_for) -> None:
    res = run_live(
        project, "run", "-d", "--geometry", "200x50", "--", "sh", "-c", "stty size"
    )
    sid = res.stdout.strip()
    _wait_exited(project, run_live, wait_for, sid)
    assert run_live(project, "cat", sid[:8]).stdout.split() == ["50", "200"]


def test_geometry_applies_to_foreground_runs(project: Path, run_live) -> None:
    # Foreground with piped stdio (no TTY) also gets the explicit size.
    run_live(
        project,
        "run",
        "-n",
        "fg",
        "--geometry",
        "120x40",
        "--",
        "sh",
        "-c",
        "stty size",
    )
    assert run_live(project, "cat", "fg").stdout.split() == ["40", "120"]


def test_geometry_rejects_malformed_values(project: Path, run_live) -> None:
    for bad in ("200", "x24", "200x", "0x24", "200x0", "axb", "80x24x10"):
        res = run_live(
            project,
            "run",
            f"--geometry={bad}",
            "--",
            "sh",
            "-c",
            "echo x",
            check=False,
        )
        assert res.returncode == 2, bad
