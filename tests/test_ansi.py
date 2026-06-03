"""ANSI strip/raw flag handling end-to-end."""

from __future__ import annotations

from pathlib import Path


# A short script that writes ANSI color codes around a word.
_ANSI_SCRIPT = (
    "printf '\\033[31mred\\033[0m\\n'; "
    "printf '\\033[1;32mbold-green\\033[0m\\n'"
)


def _record(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "colors", "--", "sh", "-c", _ANSI_SCRIPT)


def test_cat_strips_ansi_by_default_when_stdout_not_tty(project: Path, run_live) -> None:
    _record(project, run_live)
    out = run_live(project, "cat", "colors").stdout
    assert "\x1b[" not in out
    assert "red" in out and "bold-green" in out


def test_cat_raw_keeps_ansi(project: Path, run_live) -> None:
    _record(project, run_live)
    out = run_live(project, "cat", "--raw", "colors").stdout
    assert "\x1b[31m" in out
    assert "\x1b[1;32m" in out


def test_cat_explicit_strip_ansi(project: Path, run_live) -> None:
    _record(project, run_live)
    out = run_live(project, "cat", "--strip-ansi", "colors").stdout
    assert "\x1b[" not in out


def test_tail_since_always_strips(project: Path, run_live) -> None:
    _record(project, run_live)
    # --since implies --strip-ansi even without --strip-ansi flag.
    out = run_live(project, "tail", "--since", "0", "colors").stdout
    assert "\x1b[" not in out


def test_tail_since_raw_keeps_ansi(project: Path, run_live) -> None:
    _record(project, run_live)
    # --raw overrides the --since default.
    out = run_live(project, "tail", "--since", "0", "--raw", "colors").stdout
    assert "\x1b[31m" in out
