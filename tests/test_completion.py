"""Checks for `live completion-script <shell>` payloads and the
`live completion selectors|cwds` data verbs the payloads consume."""

from __future__ import annotations

from pathlib import Path

import pytest


VERBS = ("run", "ls", "cat", "head", "tail", "less", "rm", "completion-script")


@pytest.mark.parametrize(
    "shell,marker",
    [
        ("bash", "complete -F"),
        ("zsh", "#compdef live"),
        ("fish", "complete -c live"),
    ],
)
def test_completion_payload_contains_shell_hookup(
    shell: str, marker: str, run_live, tmp_path: Path
) -> None:
    out = run_live(tmp_path, "completion-script", shell).stdout
    assert out, f"{shell}: empty payload"
    assert marker in out, f"{shell}: missing hookup marker {marker!r}"
    for verb in VERBS:
        assert verb in out, f"{shell}: missing verb {verb!r}"


def test_zsh_payload_enables_option_stacking(run_live, tmp_path: Path) -> None:
    """Without `_arguments -s`, clustered short flags (`-dn`, `-ag`) are
    mistaken for the wrapped command / a selector."""
    out = run_live(tmp_path, "completion-script", "zsh").stdout
    assert "_arguments -s -S" in out  # run
    assert out.count("_arguments -s") >= 8  # run + the selector verbs


def test_older_than_value_slot_stays_owned(run_live, tmp_path: Path) -> None:
    """`rm --older-than` takes an AGE value; the zsh spec's `=` suffix and
    fish's `-r` keep selectors out of that slot."""
    zsh = run_live(tmp_path, "completion-script", "zsh").stdout
    assert "'--older-than=:" in zsh
    fish = run_live(tmp_path, "completion-script", "fish").stdout
    assert "-l older-than -r" in fish


def test_completion_selectors_lists_names_and_ids(project: Path, run_live) -> None:
    """Exited sessions are excluded by default and included with `-a`;
    both the name and the session id are offered."""
    run_live(project, "run", "-n", "gone", "--", "sh", "-c", "echo a")

    active = run_live(project, "completion", "selectors").stdout.splitlines()
    assert "gone" not in active

    everything = run_live(project, "completion", "selectors", "-a").stdout.splitlines()
    assert "gone" in everything
    [sid] = [t for t in everything if t != "gone"]
    assert len(sid) == 36, sid


def test_completion_selectors_honors_scope_flags(project: Path, run_live) -> None:
    a = project / "A"
    b = project / "B"
    a.mkdir()
    b.mkdir()
    run_live(a, "run", "-n", "in-A", "--", "sh", "-c", "echo a")

    out_of_scope = run_live(b, "completion", "selectors", "-a").stdout.splitlines()
    assert "in-A" not in out_of_scope
    scoped = run_live(
        b, "completion", "selectors", "-a", "-C", str(a)
    ).stdout.splitlines()
    assert "in-A" in scoped
    global_ = run_live(b, "completion", "selectors", "-a", "-g").stdout.splitlines()
    assert "in-A" in global_


def test_completion_cwds_lists_distinct_session_cwds(project: Path, run_live) -> None:
    a = project / "A"
    a.mkdir()
    run_live(a, "run", "--", "sh", "-c", "echo a")
    run_live(a, "run", "--", "sh", "-c", "echo b")

    out = run_live(project, "completion", "cwds").stdout.splitlines()
    assert out.count(str(a.resolve())) == 1, out


@pytest.mark.parametrize(
    "shell,rel",
    [
        ("bash", ".local/share/bash-completion/completions/live"),
        ("zsh", ".local/share/zsh/site-functions/_live"),
        ("fish", ".config/fish/completions/live.fish"),
    ],
)
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
