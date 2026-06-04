"""cwd-or-descendant scope filter and `-g`/`--global` widening.

Read verbs default to sessions whose `meta.cwd` is the caller's cwd or a
descendant. `-g` lifts the filter to all sessions in `~/.live/sessions/`.
"""

from __future__ import annotations

import json
from pathlib import Path


def _ls_names(stdout: str) -> set[str]:
    return {
        json.loads(ln).get("name")
        for ln in stdout.splitlines()
        if ln.strip()
    }


def test_ls_excludes_sibling_dir_session(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "in-A", "--", "sh", "-c", "echo a")

    out = run_live(b, "ls", "-a", "--json")
    assert out.stdout.strip() == ""


def test_ls_global_includes_sibling_dir_session(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "in-A", "--", "sh", "-c", "echo a")

    out = run_live(b, "ls", "-ag", "--json")
    assert "in-A" in _ls_names(out.stdout)


def test_ls_default_scope_includes_descendant_session(
    project: Path, run_live
) -> None:
    a = project / "A"
    sub = a / "deep" / "sub"
    sub.mkdir(parents=True)
    run_live(sub, "run", "-n", "in-sub", "--", "sh", "-c", "echo s")

    out = run_live(a, "ls", "-a", "--json")
    assert "in-sub" in _ls_names(out.stdout)


def test_cat_respects_scope(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "elsewhere", "--", "sh", "-c", "echo hi")

    miss = run_live(b, "cat", "elsewhere", check=False)
    assert miss.returncode == 2
    assert "no such session" in miss.stderr

    hit = run_live(b, "cat", "-g", "elsewhere")
    assert "hi" in hit.stdout


def test_rm_respects_scope(project: Path, run_live) -> None:
    """`rm <name>` from a sibling directory must not delete out-of-scope sessions."""
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "out-of-scope", "--", "sh", "-c", "echo a")

    miss = run_live(b, "rm", "out-of-scope", check=False)
    assert miss.returncode == 1
    assert "no such session" in miss.stderr

    # Still present from A.
    ls = run_live(a, "ls", "-a", "--json")
    assert "out-of-scope" in _ls_names(ls.stdout)

    # With -g, removal succeeds.
    rm = run_live(b, "rm", "-g", "out-of-scope")
    assert rm.returncode == 0
    ls = run_live(a, "ls", "-a", "--json")
    assert ls.stdout.strip() == ""
