"""`live rm` selector + filter composition (intersect semantics)."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from live.cli import _parse_age


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


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


def test_parse_age_iso_date() -> None:
    cutoff = _parse_age("2026-01-01")
    expected = datetime.fromisoformat("2026-01-01").timestamp()
    assert math.isclose(cutoff, expected)


def test_parse_age_iso_datetime() -> None:
    cutoff = _parse_age("2026-01-01T12:00:00")
    expected = datetime.fromisoformat("2026-01-01T12:00:00").timestamp()
    assert math.isclose(cutoff, expected)


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


def test_rm_filter_only_errors(project: Path, run_live) -> None:
    """A bare filter is not a selector; needs --all or a NAME."""
    rm = run_live(project, "rm", "--exited", check=False)
    assert rm.returncode == 2
    assert "missing selector" in rm.stderr


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

    # Both already exited (sh -c finishes immediately); --exited keeps them in.
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


# ----- intersect over orthogonal axes -----


def test_exited_and_untitled_intersect(project: Path, live_env, run_live) -> None:
    """--exited and --untitled cover orthogonal axes (status × naming).
    `--all --exited --untitled` deletes only sessions matching BOTH:
    a named-exited session, an unnamed-running one, and an unnamed-exited one;
    only the last qualifies."""
    run_live(project, "run", "-n", "named", "--", "sh", "-c", "echo x")  # named exited
    run_live(project, "run", "--", "sh", "-c", "echo y")                 # unnamed exited

    proc = subprocess.Popen(  # unnamed running
        [sys.executable, "-m", "live.cli", "run", "--", "sh", "-c", "echo go; sleep 60"],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sessions = project / ".live" / "sessions"
        assert _wait_for(lambda: sum(1 for _ in sessions.iterdir()) == 3)
        assert _wait_for(
            lambda: sum(1 for s in sessions.iterdir() if (s / "meta.json").exists()) == 3
        )

        rm = run_live(project, "rm", "--all", "--exited", "--untitled")
        assert rm.returncode == 0

        # Only the unnamed-exited session is gone; named-exited and unnamed-running remain.
        remaining = _ls_entries(project, run_live)
        names = [e.get("name") for e in remaining]
        statuses = [e["status"] for e in remaining]
        assert sorted(names, key=lambda x: (x is None, x or "")) == ["named", None]
        assert "running" in statuses
        assert "exited" in statuses
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
