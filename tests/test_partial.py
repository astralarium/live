"""Partial-line tail surfacing via tail -n +0."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_partial_line_surfaces_in_tail(
    project: Path, live_env, run_live, wait_for, wait_for_session
) -> None:
    # One complete line, a partial prompt without a newline, then sleep so the
    # partial sits there long enough for the reader to observe it.
    script = "printf 'first complete line\\n'; printf 'Continue? [Y/n] '; sleep 30"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "live.cli",
            "run",
            "-n",
            "prompt",
            "--",
            "sh",
            "-c",
            script,
        ],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sess_dir = wait_for_session()
        stream = sess_dir / "stream.0000.log"
        idx = sess_dir / "lines.0000.idx"

        def has_partial() -> bool:
            try:
                s = stream.read_bytes()
                i = idx.read_bytes()
            except FileNotFoundError:
                return False
            return len(i) == 40 and b"Continue?" in s and not s.endswith(b"\n")

        assert wait_for(has_partial, timeout=8.0), "partial state never appeared"

        poll = run_live(project, "tail", "-vn", "+0", "prompt")
        out = poll.stdout.replace("\r", "")
        assert "first complete line" in out
        assert "Continue? [Y/n] " in out
        assert "live: partial-line bytes=" in poll.stderr
        assert "age=" in poll.stderr

        # GNU fragment semantics: the open line occupies the newest -n slot.
        only_partial = run_live(project, "tail", "-n", "1", "prompt")
        assert only_partial.stdout == "Continue? [Y/n] "
        both = run_live(project, "tail", "-n", "2", "prompt")
        assert "first complete line" in both.stdout
        assert "Continue? [Y/n] " in both.stdout

        # Caught-up line cursor sits on the open line: partial, no warning.
        poll2 = run_live(project, "tail", "-vn", "+2", "prompt")
        assert poll2.stdout == "Continue? [Y/n] "
        assert "check id" not in poll2.stderr

        # A cursor past the open line emits nothing — not the partial.
        ahead = run_live(project, "tail", "-vn", "+5", "prompt")
        assert ahead.stdout == ""
        assert "check id" in ahead.stderr

        # head -n 1 is satisfied by the complete line: no partial.
        h1 = run_live(project, "head", "-n", "1", "prompt")
        assert "Continue?" not in h1.stdout
        # head -n 2 extends past the last complete line: partial included.
        h2 = run_live(project, "head", "-n", "2", "prompt")
        assert "Continue? [Y/n] " in h2.stdout

        # tail -c K is exactly the last K bytes of the stream, fragment
        # included (GNU), with the partial marker on stderr.
        k = run_live(project, "tail", "-vc", "4", "prompt", text=False)
        assert k.stdout == b"/n] "
        assert b"partial-line bytes=4" in k.stderr
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
