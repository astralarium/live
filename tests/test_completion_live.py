"""Live execution of generated completion scripts in real shells.

Sources each shell's payload, drives completion non-interactively, and checks
the offered candidates include the expected verbs and flags.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


CORE_VERBS = {"run", "ls", "cat", "tail", "rm", "init", "completion"}


def _have(shell: str) -> bool:
    return shutil.which(shell) is not None


# ----- fish -----


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_completes_verbs(run_live, tmp_path: Path) -> None:
    script = run_live(tmp_path, "completion", "fish").stdout
    payload = tmp_path / "live.fish"
    payload.write_text(script)
    out = subprocess.run(
        ["fish", "-c", f"source {payload}; complete -C 'live '"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Fish prints `verb\tdescription` per line.
    candidates = {line.split("\t", 1)[0] for line in out.splitlines() if line}
    assert CORE_VERBS <= candidates, f"missing: {CORE_VERBS - candidates}; got: {candidates}"


@pytest.mark.skipif(not _have("fish"), reason="fish not installed")
def test_fish_completes_tail_flags(run_live, tmp_path: Path) -> None:
    script = run_live(tmp_path, "completion", "fish").stdout
    payload = tmp_path / "live.fish"
    payload.write_text(script)
    out = subprocess.run(
        ["fish", "-c", f"source {payload}; complete -C 'live tail -'"],
        capture_output=True, text=True, check=True,
    ).stdout
    candidates = {line.split("\t", 1)[0] for line in out.splitlines() if line}
    # Should at least suggest -f and --since-line for `live tail -`.
    assert any(c.startswith("-f") or c == "--follow" for c in candidates), candidates
    assert any("since-line" in c for c in candidates), candidates


# ----- bash -----


_BASH_DRIVE = r"""
set +e
source {payload}
COMP_WORDS=(live "{partial}")
COMP_CWORD=1
COMP_LINE="live {partial}"
COMP_POINT=${{#COMP_LINE}}
_live_complete 2>/dev/null
printf '%s\n' "${{COMPREPLY[@]}}"
"""


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_completes_verbs(run_live, tmp_path: Path) -> None:
    script = run_live(tmp_path, "completion", "bash").stdout
    payload = tmp_path / "live.bash"
    payload.write_text(script)
    out = subprocess.run(
        ["bash", "-c", _BASH_DRIVE.format(payload=payload, partial="")],
        capture_output=True, text=True, check=True,
    ).stdout
    candidates = {ln for ln in out.split() if ln}
    assert CORE_VERBS <= candidates, f"missing: {CORE_VERBS - candidates}; got: {candidates}"


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_completes_tail_flags(run_live, tmp_path: Path) -> None:
    script = run_live(tmp_path, "completion", "bash").stdout
    payload = tmp_path / "live.bash"
    payload.write_text(script)
    # COMP_WORDS for `live tail -` is ("live", "tail", "-").
    drive = r"""
set +e
source {payload}
COMP_WORDS=(live tail "-")
COMP_CWORD=2
COMP_LINE="live tail -"
COMP_POINT=${{#COMP_LINE}}
_live_complete 2>/dev/null
printf '%s\n' "${{COMPREPLY[@]}}"
""".format(payload=payload)
    out = subprocess.run(
        ["bash", "-c", drive],
        capture_output=True, text=True, check=True,
    ).stdout
    candidates = {ln for ln in out.split() if ln}
    assert "-f" in candidates or "--follow" in candidates, candidates
    assert any("since-line" in c for c in candidates), candidates


# ----- zsh -----


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

    script = run_live(tmp_path, "completion", "fish").stdout
    payload = tmp_path / "live.fish"
    payload.write_text(script)
    out = subprocess.run(
        ["fish", "-c",
         f"source {payload}; complete -C 'live run cat {target_dir}/unique'"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "uniqueapple.txt" in out, out
    assert "uniqueberry.txt" in out, out


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_run_hands_off_via_command_offset(
    run_live, tmp_path: Path
) -> None:
    """Verify our bash script calls `_command_offset 2` after the verb + a non-flag arg.

    `_command_offset` is provided by bash-completion; we stub it here so the test
    doesn't depend on bash-completion being installed.
    """
    script = run_live(tmp_path, "completion", "bash").stdout
    payload = tmp_path / "live.bash"
    payload.write_text(script)
    drive = f"""
set +e
_command_offset() {{ echo "OFFSET_CALLED=$1"; }}
source {payload}
COMP_WORDS=(live run somecmd "")
COMP_CWORD=3
COMP_LINE="live run somecmd "
COMP_POINT=${{#COMP_LINE}}
_live_complete 2>/dev/null
"""
    out = subprocess.run(
        ["bash", "-c", drive], capture_output=True, text=True, check=True,
    ).stdout
    # somecmd is at index 2 in COMP_WORDS (live=0, run=1, somecmd=2).
    assert "OFFSET_CALLED=2" in out, f"handoff not triggered; got: {out!r}"


@pytest.mark.skipif(not _have("bash"), reason="bash not installed")
def test_bash_run_offers_own_flags_before_command(
    run_live, tmp_path: Path
) -> None:
    """Before any wrapped command is typed, `live run -<TAB>` offers our own flags
    (`-n`/`--name`/`--`) — NOT a handoff to anything else."""
    script = run_live(tmp_path, "completion", "bash").stdout
    payload = tmp_path / "live.bash"
    payload.write_text(script)
    drive = f"""
set +e
_command_offset() {{ echo "OFFSET_CALLED=$1"; }}  # should NOT fire
source {payload}
COMP_WORDS=(live run "-")
COMP_CWORD=2
COMP_LINE="live run -"
COMP_POINT=${{#COMP_LINE}}
_live_complete 2>/dev/null
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    out = subprocess.run(
        ["bash", "-c", drive], capture_output=True, text=True, check=True,
    ).stdout
    assert "OFFSET_CALLED" not in out, f"handoff fired prematurely: {out!r}"
    assert "-n" in out.split() or "--name" in out.split(), out


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_script_parses_cleanly(run_live, tmp_path: Path) -> None:
    """Catch bash-isms / syntax errors that autoload would otherwise defer."""
    script = run_live(tmp_path, "completion", "zsh").stdout
    payload = tmp_path / "_live"
    payload.write_text(script)
    # `zsh -n` parses without executing — surfaces syntax errors immediately.
    result = subprocess.run(
        ["zsh", "-n", str(payload)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"zsh parse failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(not _have("zsh"), reason="zsh not installed")
def test_zsh_registers_completion(run_live, tmp_path: Path) -> None:
    """Verify the completion script loads cleanly and registers `_live`."""
    script = run_live(tmp_path, "completion", "zsh").stdout
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
        ["zsh", "-c", inner], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out and out != "MISSING", f"zsh did not register completion: {out!r}"
