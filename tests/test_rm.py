"""`live rm` selector + filter composition (intersect semantics)."""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

import pytest

from live.cli import _parse_age


# ----- _parse_age unit tests -----


@pytest.mark.parametrize(
    "value,seconds",
    [("7d", 7 * 86400), ("12h", 12 * 3600), ("30m", 30 * 60), ("60s", 60),
     ("1.5d", 1.5 * 86400)],
)
def test_parse_age_duration(value: str, seconds: float) -> None:
    before = time.time()
    cutoff = _parse_age(value)
    after = time.time()
    assert before - seconds <= cutoff <= after - seconds


@pytest.mark.parametrize("value", ["2026-01-01", "2026-01-01T12:00:00"])
def test_parse_age_iso(value: str) -> None:
    assert math.isclose(_parse_age(value), datetime.fromisoformat(value).timestamp())


@pytest.mark.parametrize("value", ["7", "7days", "yesterday", "", "1d2h"])
def test_parse_age_rejects_bad_input(value: str) -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_age(value)


# ----- helpers -----


def _ls_entries(project: Path, run_live) -> list[dict]:
    out = run_live(project, "ls", "-a", "--json").stdout
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def _ls_names(project: Path, run_live) -> set[str | None]:
    return {e.get("name") for e in _ls_entries(project, run_live)}


# ----- guard: bare rm and filter-only rm are errors -----


def test_rm_with_nothing_errors(project: Path, run_live) -> None:
    rm = run_live(project, "rm", check=False)
    assert rm.returncode == 2
    assert "missing selector" in rm.stderr


def test_rm_exited_alone_implies_all(project: Path, run_live) -> None:
    """`rm --exited` (no selector) deletes every exited session in scope —
    --exited implies --all when no selector was given."""
    run_live(project, "run", "-n", "a", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "b", "--", "sh", "-c", "echo y")

    rm = run_live(project, "rm", "--exited")
    assert rm.returncode == 0
    assert _ls_entries(project, run_live) == []


def test_rm_untitled_alone_implies_exited_and_all(
    project: Path, run_live, spawn_run, wait_for
) -> None:
    """`rm --untitled` deletes only unnamed-AND-exited sessions; the unnamed
    running recorder is protected by the implied --exited."""
    run_live(project, "run", "-n", "named", "--", "sh", "-c", "echo x")
    run_live(project, "run", "--", "sh", "-c", "echo y")
    spawn_run()

    sessions = project / ".live" / "sessions"
    assert wait_for(
        lambda: sum(1 for s in sessions.iterdir() if (s / "meta.json").exists()) == 3
    )

    rm = run_live(project, "rm", "--untitled")
    assert rm.returncode == 0

    remaining = _ls_entries(project, run_live)
    names = sorted((e.get("name") for e in remaining), key=lambda x: (x is None, x or ""))
    statuses = {e["status"] for e in remaining}
    assert names == ["named", None]
    assert statuses == {"exited", "running"}


# ----- --all selector -----


def test_all_deletes_everything(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "a", "--", "sh", "-c", "echo 1")
    run_live(project, "run", "-n", "b", "--", "sh", "-c", "echo 2")
    run_live(project, "run", "--", "sh", "-c", "echo 3")

    rm = run_live(project, "rm", "--all")
    assert rm.returncode == 0
    assert _ls_entries(project, run_live) == []


# ----- --all + filters intersect -----


def test_all_and_exited_filter(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "old", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "new", "--", "sh", "-c", "echo y")

    # Both already exited (sh -c returns immediately); --exited matches both.
    rm = run_live(project, "rm", "--all", "--exited")
    assert rm.returncode == 0
    assert _ls_entries(project, run_live) == []


def test_all_and_untitled(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "kept", "--", "sh", "-c", "echo x")
    run_live(project, "run", "--", "sh", "-c", "echo y")
    run_live(project, "run", "--", "sh", "-c", "echo z")

    rm = run_live(project, "rm", "--all", "--untitled")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"kept"}


def test_all_with_older_than_keeps_recent(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "a", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "b", "--", "sh", "-c", "echo y")

    # 1h-old cutoff: nothing qualifies (seconds-old sessions).
    rm = run_live(project, "rm", "--all", "--older-than", "1h")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"a", "b"}

    # 0s cutoff = now: both exited sessions qualify.
    rm = run_live(project, "rm", "--all", "--older-than", "0s")
    assert rm.returncode == 0
    assert _ls_entries(project, run_live) == []


# ----- NAME selector + filters intersect -----


def test_name_with_older_than(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "alpha", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "beta", "--", "sh", "-c", "echo y")

    # Future cutoff: name matches but filter rejects → no deletes.
    rm = run_live(project, "rm", "alpha", "--older-than", "1h")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"alpha", "beta"}

    # Now cutoff: alpha gone, beta untouched (intersect bounded by NAME).
    rm = run_live(project, "rm", "alpha", "--older-than", "0s")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"beta"}


# ----- multi-NAME and running-session guard -----


def test_rm_name_matches_every_session_with_that_name(project: Path, run_live) -> None:
    """`rm <name>` deletes every session bearing that name (resolve_many)."""
    run_live(project, "run", "-n", "dup", "--", "sh", "-c", "echo 1")
    run_live(project, "run", "-n", "dup", "--", "sh", "-c", "echo 2")
    run_live(project, "run", "-n", "keep", "--", "sh", "-c", "echo 3")

    rm = run_live(project, "rm", "dup")
    assert rm.returncode == 0
    assert len(rm.stdout.strip().splitlines()) == 2
    assert _ls_names(project, run_live) == {"keep"}


def test_rm_running_session_refuses_without_force(
    project: Path, run_live, spawn_run, wait_for, wait_for_session
) -> None:
    """`rm <running-name>` without -f leaves the session intact and exits 1."""
    spawn_run("-n", "alive")
    sess_dir = wait_for_session()
    assert wait_for(lambda: (sess_dir / "meta.json").exists())

    rm = run_live(project, "rm", "alive", check=False)
    assert rm.returncode == 1
    assert "is running" in rm.stderr
    assert sess_dir.exists()
    assert _ls_names(project, run_live) == {"alive"}


# ----- -f / ignore-missing -----


def test_rm_f_ignores_missing_selector(project: Path, run_live) -> None:
    """`rm -f nonexistent` is a no-op, mirroring Unix `rm -f`."""
    run_live(project, "run", "-n", "real", "--", "sh", "-c", "echo r")
    rm = run_live(project, "rm", "-f", "nonexistent")
    assert rm.returncode == 0
    assert rm.stderr == ""
    assert _ls_names(project, run_live) == {"real"}


def test_rm_f_with_mix_of_missing_and_present(project: Path, run_live) -> None:
    """`rm -f <missing> <real>` removes the real, silently skips the missing."""
    run_live(project, "run", "-n", "real", "--", "sh", "-c", "echo r")
    rm = run_live(project, "rm", "-f", "nope", "real")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == set()


def test_rm_without_f_errors_on_missing_selector(project: Path, run_live) -> None:
    """Sanity check: without -f, a missing selector still surfaces as an error."""
    run_live(project, "run", "-n", "real", "--", "sh", "-c", "echo r")
    rm = run_live(project, "rm", "nope", check=False)
    assert rm.returncode == 1
    assert "no such session" in rm.stderr


# ----- intersect over orthogonal axes -----


def test_exited_and_untitled_intersect(
    project: Path, run_live, spawn_run, wait_for
) -> None:
    """--exited and --untitled cover orthogonal axes (status × naming).
    Of {named-exited, unnamed-exited, unnamed-running}, only the unnamed-exited
    session matches BOTH filters."""
    run_live(project, "run", "-n", "named", "--", "sh", "-c", "echo x")  # named exited
    run_live(project, "run", "--", "sh", "-c", "echo y")                 # unnamed exited
    spawn_run()                                                          # unnamed running

    sessions = project / ".live" / "sessions"
    assert wait_for(
        lambda: sum(1 for s in sessions.iterdir() if (s / "meta.json").exists()) == 3
    )

    rm = run_live(project, "rm", "--all", "--exited", "--untitled")
    assert rm.returncode == 0

    remaining = _ls_entries(project, run_live)
    names = sorted((e.get("name") for e in remaining), key=lambda x: (x is None, x or ""))
    statuses = {e["status"] for e in remaining}
    assert names == ["named", None]
    assert statuses == {"exited", "running"}
