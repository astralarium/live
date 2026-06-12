"""Live execution of generated completion scripts in real shells.

Sources each shell's payload, drives completion non-interactively, and checks
the offered candidates include the expected verbs, flags, and selectors.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest


CORE_VERBS = {"run", "ps", "cat", "head", "tail", "less", "rm", "completion"}


def _have(shell: str) -> bool:
    return shutil.which(shell) is not None


def _payload(run_live, tmp_path: Path, shell: str) -> Path:
    name = {"bash": "live.bash", "zsh": "_live", "fish": "live.fish"}[shell]
    p = tmp_path / name
    p.write_text(run_live(tmp_path, "completion-script", shell).stdout)
    return p


def _drive_bash(
    payload: Path,
    words: tuple[str, ...],
    cword: int,
    *,
    env: dict | None = None,
    cwd: Path | None = None,
    prelude: str = "",
) -> set[str]:
    """Source `payload`, run `_live_complete` against COMP_WORDS=`words`,
    and return the emitted lines (COMPREPLY plus any prelude output)."""
    words_bash = " ".join(f'"{w}"' for w in words)
    comp_line = " ".join(words)
    script = f"""
set +e
{prelude}
source {payload}
COMP_WORDS=({words_bash})
COMP_CWORD={cword}
COMP_LINE="{comp_line}"
COMP_POINT=${{#COMP_LINE}}
_live_complete 2>/dev/null
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    ).stdout
    return {ln for ln in out.splitlines() if ln}


def _drive_fish(
    payload: Path, line: str, *, env: dict | None = None, cwd: Path | None = None
) -> set[str]:
    """Drive fish's `complete -C` against the sourced payload; return the
    candidate tokens (descriptions stripped)."""
    out = subprocess.run(
        ["fish", "-c", f"source {payload}; complete -C '{line}'"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    ).stdout
    return {ln.split("\t", 1)[0] for ln in out.splitlines() if ln}


def _zsh_selectors(payload: Path, env: dict, cwd: Path, *words: str) -> set[str]:
    """Run the payload's `_live_selectors` against `words` (verb first),
    with the compsys builtins it calls stubbed out."""
    words_z = " ".join(f"'{w}'" for w in words)
    inner = (
        f"fns=$(awk '/^_live_selectors\\(\\) \\{{/,/^\\}}/' {payload}); "
        f'eval "$fns"; '
        "_values() { shift; printf '%s\\n' \"$@\"; }; "
        f"words=({words_z} ''); "
        "_live_selectors; :"  # empty result is not an error
    )
    out = subprocess.run(
        ["zsh", "-c", inner],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=str(cwd),
    ).stdout
    return {ln for ln in out.splitlines() if ln}


# ----- fish -----


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_completes_verbs(run_live, tmp_path: Path) -> None:
    payload = _payload(run_live, tmp_path, "fish")
    candidates = _drive_fish(payload, "live ")
    assert CORE_VERBS <= candidates, (
        f"missing: {CORE_VERBS - candidates}; got: {candidates}"
    )


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_completes_tail_flags(run_live, tmp_path: Path) -> None:
    payload = _payload(run_live, tmp_path, "fish")
    candidates = _drive_fish(payload, "live tail -")
    # Should at least suggest -f and -t/--time for `live tail -`.
    assert any(c.startswith("-f") or c == "--follow" for c in candidates), candidates
    assert any(c == "--time" or c == "-t" for c in candidates), candidates


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_run_falls_through_to_filename_completion(
    run_live, tmp_path: Path
) -> None:
    """`live run cat <path-prefix><TAB>` should complete filenames via fish's
    built-in `__fish_complete_subcommand`."""
    target_dir = tmp_path / "ftarget"
    target_dir.mkdir()
    (target_dir / "uniqueapple.txt").touch()
    (target_dir / "uniqueberry.txt").touch()

    payload = _payload(run_live, tmp_path, "fish")
    candidates = _drive_fish(payload, f"live run cat {target_dir}/unique")
    assert any("uniqueapple.txt" in c for c in candidates), candidates
    assert any("uniqueberry.txt" in c for c in candidates), candidates


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_run_handoff_skips_flag_values(run_live, tmp_path: Path) -> None:
    """A `-C DIR` (or other value-taking flag) before the wrapped command must
    not be mistaken for the command itself, and `run`'s own flags must not be
    offered inside the wrapped command."""
    target_dir = tmp_path / "ftarget"
    target_dir.mkdir()
    (target_dir / "uniqueapple.txt").touch()

    payload = _payload(run_live, tmp_path, "fish")
    candidates = _drive_fish(payload, f"live run -C {tmp_path} cat {target_dir}/unique")
    assert any("uniqueapple.txt" in c for c in candidates), candidates

    inside_cmd = _drive_fish(payload, "live run cat -")
    assert "--detach" not in inside_cmd, inside_cmd
    assert "--geometry" not in inside_cmd, inside_cmd


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_run_clustered_flags_hand_off(run_live, tmp_path: Path) -> None:
    """`live run -dn NAME cmd <TAB>` must complete against `cmd`, not NAME
    (README's flagship form)."""
    target_dir = tmp_path / "ftarget"
    target_dir.mkdir()
    (target_dir / "uniqueapple.txt").touch()

    payload = _payload(run_live, tmp_path, "fish")
    candidates = _drive_fish(payload, f"live run -dn server cat {target_dir}/unique")
    assert any("uniqueapple.txt" in c for c in candidates), candidates

    # Before the wrapped command, run's own flags are still offered...
    flags = _drive_fish(payload, "live run -dn server -")
    assert "--detach" in flags, flags
    # ...but not inside the wrapped command.
    inside = _drive_fish(payload, "live run -dn server cat -")
    assert "--detach" not in inside, inside


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_clustered_scope_flags_forwarded(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live ps -ag <TAB>` forwards both -a and -g; attached `-CDIR` scopes."""
    test_env = live_shim
    proj = tmp_path / "projdir"
    other = tmp_path / "otherdir"
    proj.mkdir()
    other.mkdir()
    run_live(proj, "run", "-n", "clustered", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "fish")

    # Exited and out of scope: visible only with both -a and -g.
    assert "clustered" not in _drive_fish(payload, "live ps ", env=test_env, cwd=other)
    assert "clustered" in _drive_fish(payload, "live ps -ag ", env=test_env, cwd=other)
    session_cwd = str(proj.resolve())
    assert "clustered" in _drive_fish(
        payload, f"live cat -C{session_cwd} ", env=test_env, cwd=other
    )


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_count_flag_value_suppresses_selectors(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live tail -n <TAB>` expects a line count, not a selector."""
    test_env = live_shim
    run_live(tmp_path, "run", "-n", "target", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "fish")

    assert "target" not in _drive_fish(
        payload, "live tail -n ", env=test_env, cwd=tmp_path
    )
    # Value consumed: selectors again.
    assert "target" in _drive_fish(
        payload, "live tail -n 5 ", env=test_env, cwd=tmp_path
    )


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_older_than_value_suppresses_selectors(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live rm --older-than <TAB>` expects an AGE, not a selector
    (fish's `-r` declaration owns the value slot)."""
    test_env = live_shim
    run_live(tmp_path, "run", "-n", "target", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "fish")

    assert "target" not in _drive_fish(
        payload, "live rm --older-than ", env=test_env, cwd=tmp_path
    )
    # Value consumed: selectors again.
    assert "target" in _drive_fish(
        payload, "live rm --older-than 7d ", env=test_env, cwd=tmp_path
    )


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_cwd_flag_completes_session_cwds(
    run_live, live_shim, tmp_path: Path
) -> None:
    test_env = live_shim
    proj = tmp_path / "projdir"
    proj.mkdir()
    run_live(proj, "run", "-n", "scoped", "--", "sh", "-c", "echo a")

    payload = _payload(run_live, tmp_path, "fish")
    candidates = _drive_fish(payload, "live ps -C ", env=test_env, cwd=tmp_path)
    assert str(proj.resolve()) in candidates, candidates


# ----- bash -----


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_completes_verbs(run_live, tmp_path: Path) -> None:
    payload = _payload(run_live, tmp_path, "bash")
    candidates = _drive_bash(payload, ("live", ""), cword=1)
    assert CORE_VERBS <= candidates, (
        f"missing: {CORE_VERBS - candidates}; got: {candidates}"
    )


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_completes_tail_flags(run_live, tmp_path: Path) -> None:
    payload = _payload(run_live, tmp_path, "bash")
    candidates = _drive_bash(payload, ("live", "tail", "-"), cword=2)
    assert "-f" in candidates or "--follow" in candidates, candidates
    assert any(c == "--time" or c == "-t" for c in candidates), candidates


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_run_hands_off_via_command_offset(run_live, tmp_path: Path) -> None:
    """Verify our bash script calls `_command_offset 2` after the verb + a non-flag arg.

    `_command_offset` is provided by bash-completion; we stub it here so the test
    doesn't depend on bash-completion being installed.
    """
    payload = _payload(run_live, tmp_path, "bash")
    out = _drive_bash(
        payload,
        ("live", "run", "somecmd", ""),
        cword=3,
        prelude='_command_offset() { echo "OFFSET_CALLED=$1"; }',
    )
    # somecmd is at index 2 in COMP_WORDS (live=0, run=1, somecmd=2).
    assert "OFFSET_CALLED=2" in out, f"handoff not triggered; got: {out!r}"


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_run_offers_own_flags_before_command(run_live, tmp_path: Path) -> None:
    """Before any wrapped command is typed, `live run -<TAB>` offers our own flags
    (`-n`/`--name`/`--`) — NOT a handoff to anything else."""
    payload = _payload(run_live, tmp_path, "bash")
    out = _drive_bash(
        payload,
        ("live", "run", "-"),
        cword=2,
        prelude='_command_offset() { echo "OFFSET_CALLED=$1"; }',  # should NOT fire
    )
    assert not any("OFFSET_CALLED" in ln for ln in out), (
        f"handoff fired prematurely: {out!r}"
    )
    assert "-n" in out, out
    assert "--name" in out, out


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_run_clustered_flags_hand_off(run_live, tmp_path: Path) -> None:
    """`live run -dn NAME cmd <TAB>` hands off at `cmd`, not NAME."""
    payload = _payload(run_live, tmp_path, "bash")
    prelude = '_command_offset() { echo "OFFSET_CALLED=$1"; }'

    # live=0 run=1 -dn=2 server=3 npm=4 — handoff must target index 4.
    out = _drive_bash(
        payload,
        ("live", "run", "-dn", "server", "npm", ""),
        cword=5,
        prelude=prelude,
    )
    assert "OFFSET_CALLED=4" in out, f"handoff mis-targeted; got: {out!r}"

    # Before the wrapped command, run's own flags are still offered.
    out = _drive_bash(
        payload,
        ("live", "run", "-dn", "server", ""),
        cword=4,
        prelude=prelude,
    )
    assert not any("OFFSET_CALLED" in ln for ln in out), out
    assert "--name" in out, out


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_clustered_scope_flags_forwarded(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live ps -ag <TAB>` forwards both -a and -g; attached `-CDIR` scopes."""
    test_env = live_shim
    proj = tmp_path / "projdir"
    other = tmp_path / "otherdir"
    proj.mkdir()
    other.mkdir()
    run_live(proj, "run", "-n", "clustered", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")

    def drive(words: tuple[str, ...], cword: int) -> set[str]:
        return _drive_bash(payload, words, cword, env=test_env, cwd=other)

    # Exited and out of scope: visible only with both -a and -g.
    assert "clustered" not in drive(("live", "ps", ""), cword=2)
    assert "clustered" in drive(("live", "ps", "-ag", ""), cword=3)
    session_cwd = str(proj.resolve())
    assert "clustered" in drive(("live", "cat", f"-C{session_cwd}", ""), cword=3)


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_count_flag_value_suppresses_selectors(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live tail -n <TAB>` (and friends) expect a count, not a selector."""
    test_env = live_shim
    run_live(tmp_path, "run", "-n", "target", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")

    def drive(words: tuple[str, ...], cword: int) -> set[str]:
        return _drive_bash(payload, words, cword, env=test_env, cwd=tmp_path)

    assert "target" not in drive(("live", "tail", "-n", ""), cword=3)
    assert "target" not in drive(("live", "head", "-t", ""), cword=3)
    # Cluster ending in a count flag.
    assert "target" not in drive(("live", "tail", "-fn", ""), cword=3)
    # `--lines=` arrives split on `=`.
    assert "target" not in drive(("live", "tail", "--lines", "=", ""), cword=4)
    # Value consumed: selectors again.
    assert "target" in drive(("live", "tail", "-n", "5", ""), cword=4)


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_older_than_value_suppresses_selectors(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live rm --older-than <TAB>` expects an AGE, not a selector."""
    test_env = live_shim
    run_live(tmp_path, "run", "-n", "target", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")

    def drive(words: tuple[str, ...], cword: int) -> set[str]:
        return _drive_bash(payload, words, cword, env=test_env, cwd=tmp_path)

    assert "target" not in drive(("live", "rm", "--older-than", ""), cword=3)
    # `--older-than=` arrives split on `=`.
    assert "target" not in drive(("live", "rm", "--older-than", "=", ""), cword=4)
    # Value consumed: selectors again.
    assert "target" in drive(("live", "rm", "--older-than", "7d", ""), cword=4)


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_ps_completes_only_active_sessions(
    run_live, live_env, live_shim, wait_for, tmp_path: Path
) -> None:
    """`live ps <TAB>` should suggest only running/hung sessions; `live ps -a <TAB>`
    must include exited; `live rm <TAB>` must include exited regardless of -a."""
    test_env = live_shim

    # Exited session.
    run_live(tmp_path, "run", "-n", "deadname", "--", "sh", "-c", "echo d")
    # Long-running session.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "live.cli",
            "run",
            "-n",
            "liverun",
            "--",
            "sh",
            "-c",
            "echo l; sleep 60",
        ],
        cwd=str(tmp_path),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait until the live session is registered as running.
        assert wait_for(
            lambda: "liverun" in run_live(tmp_path, "ps", "--json").stdout,
            timeout=8.0,
        ), "running session never appeared"

        payload = _payload(run_live, tmp_path, "bash")

        # `live ps <TAB>` — active only.
        ps_active = _drive_bash(
            payload, ("live", "ps", ""), cword=2, env=test_env, cwd=tmp_path
        )
        assert "liverun" in ps_active, ps_active
        assert "deadname" not in ps_active, ps_active

        # `live ps -a <TAB>` — both.
        ps_all = _drive_bash(
            payload, ("live", "ps", "-a", ""), cword=3, env=test_env, cwd=tmp_path
        )
        assert "liverun" in ps_all, ps_all
        assert "deadname" in ps_all, ps_all

        # `live rm <TAB>` — both (rm always passes -a internally).
        rm_all = _drive_bash(
            payload, ("live", "rm", ""), cword=2, env=test_env, cwd=tmp_path
        )
        assert "liverun" in rm_all, rm_all
        assert "deadname" in rm_all, rm_all
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_ids_offered_only_without_name_match(
    run_live, live_shim, tmp_path: Path, wait_for_session
) -> None:
    """The typed prefix is forwarded: while a name matches it, the session id
    stays hidden; a prefix matching no name falls back to ids."""
    test_env = live_shim
    run_live(tmp_path, "run", "-n", "named", "--", "sh", "-c", "echo a")
    sid = wait_for_session().name
    payload = _payload(run_live, tmp_path, "bash")

    def drive(words: tuple[str, ...], cword: int) -> set[str]:
        return _drive_bash(payload, words, cword, env=test_env, cwd=tmp_path)

    offered = drive(("live", "rm", ""), cword=2)
    assert "named" in offered, offered
    assert sid not in offered, offered

    fallback = drive(("live", "rm", sid[:8]), cword=2)
    assert fallback == {sid}, fallback


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_numeric_values_dont_break_selector_completion(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live head -n -3 <TAB>` (and friends) must still complete selectors.
    Regression: the `-3` token must not be mistaken for a flag during dispatch."""
    test_env = live_shim

    run_live(tmp_path, "run", "-n", "target", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")

    def drive(words: tuple[str, ...], cword: int) -> set[str]:
        return _drive_bash(payload, words, cword, env=test_env, cwd=tmp_path)

    # `live head -n -3 <TAB>` — cur=""; expect selectors offered, not flags.
    assert "target" in drive(("live", "head", "-n", "-3", ""), cword=4)

    # `live head -n +3 <TAB>` — noop sign on head; still selectors.
    assert "target" in drive(("live", "head", "-n", "+3", ""), cword=4)

    # `live tail -c +5 <TAB>` — byte cursor; still selectors.
    assert "target" in drive(("live", "tail", "-c", "+5", ""), cword=4)

    # `live tail -c -5 <TAB>` — noop sign on tail; still selectors.
    assert "target" in drive(("live", "tail", "-c", "-5", ""), cword=4)


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_cwd_flag_completes_session_cwds(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`live ps -C <TAB>` offers the cwds of recorded sessions; a typed
    `-C <dir>` scopes subsequent selector completion to that directory."""
    test_env = live_shim
    proj = tmp_path / "projdir"
    other = tmp_path / "otherdir"
    proj.mkdir()
    other.mkdir()
    run_live(proj, "run", "-n", "scoped", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")
    session_cwd = str(proj.resolve())

    def drive(words: tuple[str, ...], cword: int) -> set[str]:
        return _drive_bash(payload, words, cword, env=test_env, cwd=other)

    # `live ps -C <TAB>` — the recorded session's cwd is offered.
    assert session_cwd in drive(("live", "ps", "-C", ""), cword=3)

    # `live cat <TAB>` from an unrelated dir — out of scope, no selectors.
    assert "scoped" not in drive(("live", "cat", ""), cword=2)

    # `live cat -C <dir> <TAB>` — selectors scoped to <dir>.
    assert "scoped" in drive(("live", "cat", "-C", session_cwd, ""), cword=4)

    # `live cat --cwd=<dir> <TAB>` — readline splits on `=` into
    # ("--cwd" "=" <dir>); the scope must still be honored.
    assert "scoped" in drive(("live", "cat", "--cwd", "=", session_cwd, ""), cword=5)


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_cwd_completion_prefers_session_cwds(
    run_live, live_shim, tmp_path: Path
) -> None:
    """While a session cwd matches the typed prefix, generic directory
    completion is suppressed; it kicks in only as a fallback."""
    test_env = live_shim
    proj = tmp_path / "projdir"
    local = tmp_path / "localdir"
    proj.mkdir()
    local.mkdir()
    run_live(proj, "run", "-n", "scoped", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")

    # Empty prefix: session cwds only, no local-dir noise.
    offered = _drive_bash(
        payload, ("live", "ps", "-C", ""), cword=3, env=test_env, cwd=tmp_path
    )
    assert str(proj.resolve()) in offered, offered
    assert "localdir" not in offered, offered

    # Prefix matching no session cwd: plain directory completion.
    fallback = _drive_bash(
        payload, ("live", "ps", "-C", "local"), cword=3, env=test_env, cwd=tmp_path
    )
    assert "localdir" in fallback, fallback


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_cwd_completion_prefers_session_cwds(
    run_live, live_shim, tmp_path: Path
) -> None:
    test_env = live_shim
    proj = tmp_path / "projdir"
    local = tmp_path / "localdir"
    proj.mkdir()
    local.mkdir()
    run_live(proj, "run", "-n", "scoped", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "fish")

    offered = _drive_fish(payload, "live ps -C ", env=test_env, cwd=tmp_path)
    assert str(proj.resolve()) in offered, offered
    assert not any(c.rstrip("/") == "localdir" for c in offered), offered

    fallback = _drive_fish(payload, "live ps -C local", env=test_env, cwd=tmp_path)
    assert any("localdir" in c for c in fallback), fallback


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_cwd_with_spaces_completes_and_scopes(
    run_live, live_shim, tmp_path: Path
) -> None:
    """Session cwds containing spaces survive `-C` value completion as a
    single candidate and scope selector completion correctly."""
    test_env = live_shim
    proj = tmp_path / "my proj"
    proj.mkdir()
    run_live(proj, "run", "-n", "spacey", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "bash")
    session_cwd = str(proj.resolve())

    offered = _drive_bash(
        payload, ("live", "ps", "-C", ""), cword=3, env=test_env, cwd=tmp_path
    )
    assert session_cwd in offered, offered

    scoped = _drive_bash(
        payload,
        ("live", "cat", "-C", session_cwd, ""),
        cword=4,
        env=test_env,
        cwd=tmp_path,
    )
    assert "spacey" in scoped, scoped


# ----- update-shell loader stubs -----


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_loader_stub_round_trips(run_live, live_shim, tmp_path: Path) -> None:
    """Sourcing the installed stub evals the real payload from `live`."""
    test_env = live_shim
    run_live(tmp_path, "update-shell", "bash")
    stub = tmp_path / ".local/share/bash-completion/completions/live"
    candidates = _drive_bash(stub, ("live", ""), cword=1, env=test_env)
    assert CORE_VERBS <= candidates, (
        f"missing: {CORE_VERBS - candidates}; got: {candidates}"
    )


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_loader_stub_round_trips(run_live, live_shim, tmp_path: Path) -> None:
    test_env = live_shim
    run_live(tmp_path, "update-shell", "fish")
    stub = tmp_path / ".config/fish/completions/live.fish"
    candidates = _drive_fish(stub, "live ", env=test_env)
    assert CORE_VERBS <= candidates, (
        f"missing: {CORE_VERBS - candidates}; got: {candidates}"
    )


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_loader_stub_round_trips(
    run_live, live_shim, live_env, tmp_path: Path
) -> None:
    """Autoloading the stub `_live` evals the real payload: the helper
    functions exist after the first invocation."""
    test_env = live_shim
    live_env["FPATH"] = "/nonexistent"  # pin the fallback install path
    run_live(tmp_path, "update-shell", "zsh")
    stub = tmp_path / ".local/share/zsh/site-functions/_live"
    assert stub.is_file()
    # `_live` runs outside a completion context, so `_arguments` fails after
    # the eval has defined the payload's functions — silence and ignore it.
    inner = (
        f"fpath=({stub.parent} $fpath); "
        "autoload -Uz compinit; "
        "compinit -u -d ${ZDOTDIR:-/tmp}/.zcompdump.$$; "
        "_live 2>/dev/null; "
        "(( $+functions[_live_selectors] )) && print LOADED"
    )
    out = subprocess.run(
        ["zsh", "-c", inner], capture_output=True, text=True, env=test_env
    ).stdout
    assert "LOADED" in out, out


# ----- zsh -----


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_script_parses_cleanly(run_live, tmp_path: Path) -> None:
    """Catch bash-isms / syntax errors that autoload would otherwise defer."""
    payload = _payload(run_live, tmp_path, "zsh")
    # `zsh -n` parses without executing — surfaces syntax errors immediately.
    result = subprocess.run(
        ["zsh", "-n", str(payload)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"zsh parse failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_registers_completion(run_live, tmp_path: Path) -> None:
    """Verify the completion script loads cleanly and registers `_live`."""
    script = run_live(tmp_path, "completion-script", "zsh").stdout
    fpath_dir = tmp_path / "zfunc"
    fpath_dir.mkdir()
    (fpath_dir / "_live").write_text(script)
    inner = (
        f"fpath=({fpath_dir} $fpath); "
        "autoload -Uz compinit; "
        "compinit -u -d ${ZDOTDIR:-/tmp}/.zcompdump.$$; "
        # Print the function bound to `live` -> should be `_live`.
        "print -- ${_comps[live]:-MISSING}"
    )
    out = subprocess.run(
        ["zsh", "-c", inner],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out and out != "MISSING", f"zsh did not register completion: {out!r}"


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_selectors_honor_all_cwd_forms(run_live, live_shim, tmp_path: Path) -> None:
    """`_live_selectors` recognizes `-C <dir>`, attached `-C<dir>` (the form
    zsh's own `-C+` spec inserts), and `--cwd=<dir>`."""
    test_env = live_shim
    proj = tmp_path / "projdir"
    other = tmp_path / "otherdir"
    proj.mkdir()
    other.mkdir()
    run_live(proj, "run", "-n", "zscoped", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "zsh")
    session_cwd = str(proj.resolve())

    def selectors(*words: str) -> set[str]:
        return _zsh_selectors(payload, test_env, other, *words)

    assert "zscoped" not in selectors("cat")
    assert "zscoped" in selectors("cat", "-C", session_cwd)
    assert "zscoped" in selectors("cat", f"-C{session_cwd}")
    assert "zscoped" in selectors("cat", f"--cwd={session_cwd}")


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_selectors_handle_clustered_flags(
    run_live, live_shim, tmp_path: Path
) -> None:
    """`-ag` sets both scope flags; `-aC <dir>` and attached `-aC<dir>` scope."""
    test_env = live_shim
    proj = tmp_path / "projdir"
    other = tmp_path / "otherdir"
    proj.mkdir()
    other.mkdir()
    run_live(proj, "run", "-n", "zclust", "--", "sh", "-c", "echo a")
    payload = _payload(run_live, tmp_path, "zsh")
    session_cwd = str(proj.resolve())

    def selectors(*words: str) -> set[str]:
        return _zsh_selectors(payload, test_env, other, *words)

    # Exited and out of scope: visible only with both -a and -g.
    assert "zclust" not in selectors("ps")
    assert "zclust" in selectors("ps", "-ag")
    assert "zclust" in selectors("ps", "-aC", session_cwd)
    assert "zclust" in selectors("ps", f"-aC{session_cwd}")
