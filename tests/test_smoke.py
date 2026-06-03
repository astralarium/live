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

    since = run_live(project, "tail", "--since-line", "1", "smoke")
    text = since.stdout.replace("\r", "")
    assert "two" in text and "three" in text
    assert "one" not in text.split("\n", 1)[0]
    assert f"id={info['id']}" in since.stderr
    assert "at-line=3" in since.stderr
    assert "exit-code=0" in since.stderr

    missing = run_live(project, "cat", "nope", check=False)
    assert missing.returncode == 2
    assert "no such session" in missing.stderr

    rm = run_live(project, "rm", "smoke")
    assert rm.returncode == 0
    ls_after = run_live(project, "ls", "-a", "--json")
    assert ls_after.stdout.strip() == ""
