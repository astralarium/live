"""End-to-end smoke test exercising run/ls/cat/tail/rm."""

from __future__ import annotations

import json
from pathlib import Path


def test_full_cycle(project: Path, run_live) -> None:
    proc = run_live(
        project,
        "run",
        "-n",
        "smoke",
        "--",
        "sh",
        "-c",
        "echo one; echo two; echo three",
    )
    assert proc.returncode == 0

    ls = run_live(project, "ls", "-a", "--json")
    lines = [ln for ln in ls.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    info = json.loads(lines[0])
    assert info["name"] == "smoke"
    assert info["status"] == "exited"
    assert info["exitCode"] == 0
    assert info["lastLine"] == 3
    assert info["count"] == 3

    cat = run_live(project, "cat", "smoke")
    text = cat.stdout.replace("\r", "")
    assert "one\ntwo\nthree\n" in text

    # Unix tail: `+2` starts at line 2 (inclusive), skipping line 1 ("one").
    since = run_live(project, "tail", "-vn", "+2", "smoke")
    text = since.stdout.replace("\r", "")
    assert "two" in text and "three" in text
    assert "one" not in text.split("\n", 1)[0]
    assert f"id={info['id']}" in since.stderr
    assert "next-line=4" in since.stderr
    assert "last-time=" in since.stderr
    assert "next-byte=" in since.stderr
    assert "exit-code=0" in since.stderr

    missing = run_live(project, "cat", "nope", check=False)
    assert missing.returncode == 1
    assert "no such session" in missing.stderr

    rm = run_live(project, "rm", "smoke")
    assert rm.returncode == 0
    ls_after = run_live(project, "ls", "-a", "--json")
    assert ls_after.stdout.strip() == ""


def test_tail_n_plus_is_inclusive_unix_semantics(project: Path, run_live) -> None:
    """`tail -n +N` emits lines with n >= N (Unix); `+1` returns all lines."""
    run_live(project, "run", "-n", "u", "--", "sh", "-c", "echo a; echo b; echo c")
    # +1 is inclusive -> all three lines.
    out = run_live(project, "tail", "-n", "+1", "u").stdout.replace("\r", "")
    assert out.splitlines() == ["a", "b", "c"]
    # +3 emits only line 3.
    out = run_live(project, "tail", "-n", "+3", "u").stdout.replace("\r", "")
    assert out.splitlines() == ["c"]
    # +4 is "caught up" (one past lastLine=3): empty, no warning.
    caught = run_live(project, "tail", "-vn", "+4", "u")
    assert caught.stdout == ""
    assert "check id" not in caught.stderr
    # +5 is genuinely ahead: empty + warning.
    ahead = run_live(project, "tail", "-vn", "+5", "u")
    assert ahead.stdout == ""
    assert "from-line=5 > next-line=4" in ahead.stderr
