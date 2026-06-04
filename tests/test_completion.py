"""Sanity checks for `live completion <shell>` payloads.

Verifies each shell's script: emits non-empty content, mentions our verbs,
references the per-shell completion hookup mechanism, and writes to stdout.
"""

from __future__ import annotations

from pathlib import Path

import pytest


VERBS = ("run", "ls", "cat", "tail", "rm", "completion")


@pytest.mark.parametrize("shell,marker", [
    ("bash", "complete -F"),
    ("zsh", "#compdef live"),
    ("fish", "complete -c live"),
])
def test_completion_payload_contains_shell_hookup(
    shell: str, marker: str, run_live, tmp_path: Path
) -> None:
    out = run_live(tmp_path, "completion", shell).stdout
    assert out, f"{shell}: empty payload"
    assert marker in out, f"{shell}: missing hookup marker {marker!r}"
    for verb in VERBS:
        assert verb in out, f"{shell}: missing verb {verb!r}"


def test_completion_unknown_shell_errors(run_live, tmp_path: Path) -> None:
    # argparse rejects this with exit 2 before our handler runs.
    result = run_live(tmp_path, "completion", "fakesh", check=False)
    assert result.returncode != 0


@pytest.mark.parametrize("shell,rel", [
    ("bash", ".local/share/bash-completion/completions/live"),
    ("zsh", ".local/share/zsh/site-functions/_live"),
    ("fish", ".config/fish/completions/live.fish"),
])
def test_update_shell_writes_completion(
    shell: str, rel: str, run_live, live_env, tmp_path: Path
) -> None:
    # FPATH=/nonexistent pins the zsh fallback path (no writable fpath dir,
    # no zsh -ic spawn).
    live_env["FPATH"] = "/nonexistent"
    result = run_live(tmp_path, "update-shell", shell)
    dst = tmp_path / rel
    assert dst.is_file(), f"{shell}: expected file at {dst}"
    assert dst.read_text(), f"{shell}: payload is empty"
    assert str(dst) in result.stdout


def test_update_shell_zsh_uses_writable_fpath_dir(
    run_live, live_env, tmp_path: Path
) -> None:
    fpath_dir = tmp_path / "my-zfunc"
    fpath_dir.mkdir()
    live_env["FPATH"] = f"{fpath_dir}:{tmp_path}/not-real"
    result = run_live(tmp_path, "update-shell", "zsh")
    dst = fpath_dir / "_live"
    assert dst.is_file()
    assert str(dst) in result.stdout
    assert "add to ~/.zshrc" not in result.stdout


def test_update_shell_detects_from_env(run_live, live_env, tmp_path: Path) -> None:
    live_env["SHELL"] = "/usr/bin/fish"
    result = run_live(tmp_path, "update-shell")
    dst = tmp_path / ".config/fish/completions/live.fish"
    assert dst.is_file()
    assert str(dst) in result.stdout
