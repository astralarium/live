"""`live stop`: terminate running sessions, keep recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _ls_entries(project: Path, run_live) -> list[dict]:
    out = run_live(project, "ls", "-a", "--json").stdout
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def _statuses(project: Path, run_live) -> dict[str, str]:
    return {e["id"]: e["status"] for e in _ls_entries(project, run_live)}


def test_stop_by_name_keeps_recording(
    project: Path, run_live, spawn_run, wait_for
) -> None:
    proc = spawn_run("-n", "srv")
    assert wait_for(
        lambda: "running" in _statuses(project, run_live).values()
    )

    res = run_live(project, "stop", "srv")
    [sid] = res.stdout.split()
    assert _statuses(project, run_live)[sid] == "exited"

    # Recording survives the stop, unlike `rm -f`.
    assert "go" in run_live(project, "cat", "srv").stdout
    # The foreground `live run` wrapper exits too.
    assert proc.wait(timeout=10) is not None


def test_stop_by_uuid_prefix(project: Path, run_live, spawn_run, wait_for) -> None:
    spawn_run("-n", "srv")
    assert wait_for(
        lambda: "running" in _statuses(project, run_live).values()
    )
    [sid] = _statuses(project, run_live)
    res = run_live(project, "stop", sid[:8])
    assert res.stdout.strip() == sid


def test_stop_all(project: Path, run_live, wait_for) -> None:
    ids = {
        run_live(
            project, "run", "-d", "-n", name, "--", "sh", "-c", "sleep 60"
        ).stdout.strip()
        for name in ("a", "b")
    }
    res = run_live(project, "stop", "--all")
    assert set(res.stdout.split()) == ids
    assert set(_statuses(project, run_live).values()) == {"exited"}


def test_stop_kills_term_ignoring_child(project: Path, run_live, wait_for) -> None:
    res = run_live(
        project, "run", "-d", "-n", "stubborn", "--",
        "sh", "-c", 'trap "" TERM; echo $$; sleep 60',
    )
    sid = res.stdout.strip()
    assert wait_for(lambda: run_live(project, "cat", "stubborn").stdout.strip())
    child_pid = int(run_live(project, "cat", "stubborn").stdout.split()[0])

    # Stop returns only once the recorder's flock is released; the recorder
    # escalates the ignored SIGTERM to a process-group SIGKILL within its
    # grace period, so the wrapped command must be gone too.
    res = run_live(project, "stop", "stubborn")
    assert res.stdout.strip() == sid
    assert _statuses(project, run_live)[sid] == "exited"

    def _child_dead() -> bool:
        try:
            os.kill(child_pid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    assert wait_for(_child_dead)


def test_stop_exited_session_errors(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "done", "--", "sh", "-c", "echo x")
    res = run_live(project, "stop", "done", check=False)
    assert res.returncode == 1
    assert "not running" in res.stderr


def test_stop_missing_selector_errors(project: Path, run_live) -> None:
    res = run_live(project, "stop", "nope", check=False)
    assert res.returncode == 1
    assert "no such session" in res.stderr


def test_stop_with_nothing_errors(project: Path, run_live) -> None:
    res = run_live(project, "stop", check=False)
    assert res.returncode == 2
    assert "missing selector" in res.stderr
