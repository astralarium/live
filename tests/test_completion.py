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
