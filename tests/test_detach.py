"""`live run -d`: detached recording + named-run conflict check."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


def _ls_entries(project: Path, run_live) -> list[dict]:
    out = run_live(project, "ls", "-a", "--json").stdout
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def _entry(project: Path, run_live, session_id: str) -> dict | None:
    for e in _ls_entries(project, run_live):
        if e["id"] == session_id:
            return e
    return None


@pytest.fixture
def stop_all(project: Path, run_live):
    """Stop any detached recorders left running at teardown."""
    yield
    run_live(project, "stop", "--all", check=False)


def test_detach_prints_uuid_and_session_is_running(
    project: Path, run_live, wait_for, stop_all
) -> None:
    res = run_live(
        project, "run", "-d", "-n", "job", "--", "sh", "-c", "echo go; sleep 60"
    )
    sid = res.stdout.strip()
    assert str(uuid.UUID(sid)) == sid

    # Session dir + lock exist before `run -d` returns: visible immediately.
    entry = _entry(project, run_live, sid)
    assert entry is not None
    assert entry["status"] == "running"
    assert entry["name"] == "job"

    # The launching `live` process is gone, yet output accrues.
    assert wait_for(lambda: "go" in run_live(project, "cat", "job").stdout)


def test_detach_records_exit_code(project: Path, run_live, wait_for) -> None:
    res = run_live(project, "run", "-d", "--", "sh", "-c", "echo go; exit 7")
    sid = res.stdout.strip()

    assert wait_for(
        lambda: (_entry(project, run_live, sid) or {}).get("status") == "exited"
    )
    assert _entry(project, run_live, sid)["exitCode"] == 7


def test_named_run_conflicts_while_running(
    project: Path, run_live, wait_for, stop_all
) -> None:
    first = run_live(
        project, "run", "-d", "-n", "job", "--", "sh", "-c", "echo go; sleep 60"
    )
    sid = first.stdout.strip()

    for extra in ((), ("-d",)):
        clash = run_live(
            project, "run", *extra, "-n", "job", "--", "sh", "-c", "echo x",
            check=False,
        )
        assert clash.returncode == 1
        assert "already running" in clash.stderr
        assert "live stop job" in clash.stderr

    # Unnamed runs and other names are unaffected.
    run_live(project, "run", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "other", "--", "sh", "-c", "echo x")

    # Conflict clears once the session is stopped.
    run_live(project, "stop", sid)
    run_live(project, "run", "-n", "job", "--", "sh", "-c", "echo x")


def test_named_run_conflict_spans_ancestors_and_descendants(
    project: Path, run_live, stop_all
) -> None:
    a = project / "a"
    b = project / "b"
    sub = a / "sub"
    sub.mkdir(parents=True)
    b.mkdir()
    run_live(a, "run", "-d", "-n", "job", "--", "sh", "-c", "sleep 60")

    # Sibling dir: disjoint scopes, same name allowed.
    run_live(b, "run", "-n", "job", "--", "sh", "-c", "echo x")

    # Ancestor dir: its scope sees the run in `a`, so it may stop it.
    clash = run_live(
        project, "run", "-n", "job", "--", "sh", "-c", "echo x", check=False
    )
    assert clash.returncode == 1
    assert "already running" in clash.stderr
    assert "live stop" in clash.stderr

    # Descendant dir: the parent's run is out of scope — no stop hint.
    clash = run_live(
        sub, "run", "-n", "job", "--", "sh", "-c", "echo x", check=False
    )
    assert clash.returncode == 1
    assert "already running in ancestor" in clash.stderr
    assert "live stop" not in clash.stderr


def test_concurrent_named_runs_admit_exactly_one(
    project: Path, live_env, run_live, stop_all
) -> None:
    # The conflict check and session creation hold a global name lock, so
    # racing starts serialize: one wins, the rest error out.
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "live.cli", "run", "-d", "-n", "race",
             "--", "sh", "-c", "sleep 60"],
            cwd=str(project),
            env=live_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    errs = [p.communicate()[1] for p in procs]
    codes = [p.returncode for p in procs]
    assert codes.count(0) == 1, (codes, errs)
    assert sum("already running" in e for e in errs) == len(procs) - 1

    entries = _ls_entries(project, run_live)
    assert sum(e["name"] == "race" for e in entries) == 1


def test_detach_with_closed_std_fds(
    project: Path, live_env, run_live, wait_for, stop_all
) -> None:
    # With fds 0-2 closed, the report pipe must not land on 0-2 where the
    # child's /dev/null dup2 dance would clobber it.
    res = subprocess.run(
        ["sh", "-c",
         'exec "$1" -m live.cli run -d -n closedfd -- '
         'sh -c "echo ok; sleep 60" <&- >&- 2>&-',
         "sh", sys.executable],
        cwd=str(project),
        env=live_env,
    )
    assert res.returncode == 0
    entry = next(
        (e for e in _ls_entries(project, run_live) if e["name"] == "closedfd"),
        None,
    )
    assert entry is not None
    assert entry["status"] == "running"
    assert wait_for(lambda: "ok" in run_live(project, "cat", "closedfd").stdout)


@pytest.mark.parametrize(
    "name", ["has space", "a/b", "a:b", "café", "a$b", "-lead", ""]
)
def test_run_rejects_unsafe_name(project: Path, run_live, name: str) -> None:
    res = run_live(
        project, "run", f"--name={name}", "--", "sh", "-c", "echo x", check=False
    )
    assert res.returncode == 2


@pytest.mark.parametrize("name", ["dev-server", "a.b_c", "X9", ".hidden", "0"])
def test_run_accepts_safe_name(project: Path, run_live, name: str) -> None:
    run_live(project, "run", "-n", name, "--", "sh", "-c", "echo x")
