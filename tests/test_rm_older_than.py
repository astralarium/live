"""`live rm --older-than` filter."""

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


# ----- integration: --older-than filters deletions -----


def _ls_names(project: Path, run_live) -> set[str]:
    out = run_live(project, "ls", "-a", "--json").stdout
    return {json.loads(ln)["name"] for ln in out.splitlines() if ln.strip()}


def test_older_than_with_all_exited_keeps_recent(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "old", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "new", "--", "sh", "-c", "echo y")

    # `--older-than 1h` selects sessions exited >1h ago; both are seconds old.
    rm = run_live(project, "rm", "--all-exited", "--older-than", "1h")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"old", "new"}

    # `--older-than 0s` cutoff = now → both exited sessions qualify.
    rm = run_live(project, "rm", "--all-exited", "--older-than", "0s")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == set()


def test_older_than_with_name_selector(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "alpha", "--", "sh", "-c", "echo x")
    run_live(project, "run", "-n", "beta", "--", "sh", "-c", "echo y")

    # Name selector + cutoff in the future: nothing qualifies.
    rm = run_live(project, "rm", "alpha", "--older-than", "1h")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"alpha", "beta"}

    # Name selector + cutoff = now: alpha is gone; beta untouched.
    rm = run_live(project, "rm", "alpha", "--older-than", "0s")
    assert rm.returncode == 0
    assert _ls_names(project, run_live) == {"beta"}
