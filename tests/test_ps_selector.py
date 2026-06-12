"""`live ps SELECTOR` — optional NAME or UUID-prefix filter."""

from __future__ import annotations

import json
from pathlib import Path


def _ids(stdout: str) -> list[str]:
    return [json.loads(ln)["id"] for ln in stdout.splitlines() if ln.strip()]


def test_ps_selector_filters_by_name(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "alpha", "--", "sh", "-c", "echo a")
    run_live(project, "run", "-n", "beta", "--", "sh", "-c", "echo b")

    out = run_live(project, "ps", "-a", "--json", "alpha")
    ids = _ids(out.stdout)
    assert len(ids) == 1
    assert json.loads(out.stdout.splitlines()[0])["name"] == "alpha"


def test_ps_selector_filters_by_uuid_prefix(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "one", "--", "sh", "-c", "echo o")
    run_live(project, "run", "-n", "two", "--", "sh", "-c", "echo t")

    all_out = run_live(project, "ps", "-a", "--json")
    rows = [json.loads(ln) for ln in all_out.stdout.splitlines() if ln.strip()]
    pick = rows[0]
    others = [r["id"] for r in rows[1:]]
    # Smallest prefix of pick.id that disambiguates from every other id.
    k = 1
    while any(o.startswith(pick["id"][:k]) for o in others):
        k += 1
    prefix = pick["id"][:k]

    out = run_live(project, "ps", "-a", "--json", prefix)
    ids = _ids(out.stdout)
    assert ids == [pick["id"]]


def test_ps_selector_no_match_is_empty_not_error(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "real", "--", "sh", "-c", "echo r")
    out = run_live(project, "ps", "-a", "--json", "nope")
    assert out.returncode == 0
    assert out.stdout.strip() == ""
