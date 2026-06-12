"""cwd-or-descendant scope filter, `-g`/`--global` widening, and `-C`/`--cwd`.

Read verbs default to sessions whose `meta.cwd` is the caller's cwd or a
descendant. `-g` lifts the filter to all sessions in `~/.live/sessions/`;
`-C PATH` scopes to PATH instead of the caller's cwd. On `run`, `-C` is the
child's working directory and the session's scope.
"""

from __future__ import annotations

import json
from pathlib import Path


def _ps_names(stdout: str) -> set[str]:
    return {json.loads(ln).get("name") for ln in stdout.splitlines() if ln.strip()}


def test_ps_excludes_sibling_dir_session(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "in-A", "--", "sh", "-c", "echo a")

    out = run_live(b, "ps", "-a", "--json")
    assert out.stdout.strip() == ""


def test_ps_global_includes_sibling_dir_session(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "in-A", "--", "sh", "-c", "echo a")

    out = run_live(b, "ps", "-ag", "--json")
    assert "in-A" in _ps_names(out.stdout)


def test_ps_default_scope_includes_descendant_session(project: Path, run_live) -> None:
    a = project / "A"
    sub = a / "deep" / "sub"
    sub.mkdir(parents=True)
    run_live(sub, "run", "-n", "in-sub", "--", "sh", "-c", "echo s")

    out = run_live(a, "ps", "-a", "--json")
    assert "in-sub" in _ps_names(out.stdout)


def test_cat_respects_scope(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "elsewhere", "--", "sh", "-c", "echo hi")

    miss = run_live(b, "cat", "elsewhere", check=False)
    assert miss.returncode == 1
    assert "no such session" in miss.stderr

    hit = run_live(b, "cat", "-g", "elsewhere")
    assert "hi" in hit.stdout


def test_ps_cwd_flag_scopes_to_other_dir(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "in-A", "--", "sh", "-c", "echo a")

    out = run_live(b, "ps", "-a", "-C", str(a), "--json")
    assert "in-A" in _ps_names(out.stdout)


def test_cat_cwd_flag_scopes_to_other_dir(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "elsewhere", "--", "sh", "-c", "echo hi")

    hit = run_live(b, "cat", "-C", str(a), "elsewhere")
    assert "hi" in hit.stdout


def test_run_cwd_flag_runs_and_scopes_there(project: Path, run_live) -> None:
    """`run -C A` runs the child in A and records A as the session's scope."""
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(b, "run", "-n", "via-C", "-C", str(a), "--", "pwd")

    # Scoped to A, not to the invoking directory B.
    assert "via-C" in _ps_names(run_live(a, "ps", "-a", "--json").stdout)
    assert run_live(b, "ps", "-a", "--json").stdout.strip() == ""

    # The child actually ran in A.
    cat = run_live(a, "cat", "via-C")
    assert str(a.resolve()) in cat.stdout


def test_run_cwd_flag_missing_dir_errors(project: Path, run_live) -> None:
    miss = run_live(
        project,
        "run",
        "-C",
        str(project / "nope"),
        "--",
        "sh",
        "-c",
        "echo hi",
        check=False,
    )
    assert miss.returncode == 2
    assert "no such directory" in miss.stderr


def test_cwd_and_global_flags_conflict(project: Path, run_live) -> None:
    out = run_live(project, "ps", "-g", "-C", str(project), check=False)
    assert out.returncode == 2


def test_cwd_flag_rejects_empty_value(project: Path, run_live) -> None:
    """`-C ""` (e.g. an unset shell variable) must error, not silently
    resolve to the invoking directory."""
    miss = run_live(project, "run", "-C", "", "--", "sh", "-c", "echo hi", check=False)
    assert miss.returncode == 2
    assert "expected a directory path" in miss.stderr

    out = run_live(project, "ps", "-C", "", check=False)
    assert out.returncode == 2


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
    ps = run_live(a, "ps", "-a", "--json")
    assert "out-of-scope" in _ps_names(ps.stdout)

    # With -g, removal succeeds.
    rm = run_live(b, "rm", "-g", "out-of-scope")
    assert rm.returncode == 0
    ps = run_live(a, "ps", "-a", "--json")
    assert ps.stdout.strip() == ""
