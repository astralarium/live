"""tail -f follow mode."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_follow_streams_lines_as_they_arrive(project: Path, live_env, wait_for) -> None:
    # A recorder that emits one line per ~150 ms for 2 seconds, then exits.
    script = (
        "for i in 1 2 3 4 5 6; do "
        "echo follow-line-$i; "
        "sleep 0.15; "
        "done"
    )
    rec = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "stream", "--",
         "sh", "-c", script],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait until the session exists.
        sessions = project / ".live" / "sessions"
        assert wait_for(lambda: sessions.exists() and any(sessions.iterdir()))
        # Wait until at least one line is indexed so we have something to follow.
        [sess] = list(sessions.iterdir())
        idx = sess / "lines.0000.idx"
        assert wait_for(lambda: idx.exists() and idx.stat().st_size >= 16,
                        timeout=5.0)

        # Start the follower; should pick up subsequent lines and exit when recorder does.
        follower = subprocess.Popen(
            [sys.executable, "-m", "live.cli", "tail", "-f", "stream"],
            cwd=str(project),
            env=live_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = follower.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            follower.kill()
            stdout, stderr = follower.communicate()
            pytest.fail("follower did not exit when recorder finished")

        body = stdout.replace("\r", "")
        # At least the last few lines must appear (-f always emits as they arrive).
        assert "follow-line-6" in body, body
        assert "follow-line-5" in body, body
        # Exit trailer.
        assert "exit-code=0" in stderr
        assert "next-line=" in stderr
    finally:
        if rec.poll() is None:
            rec.kill()
            rec.wait(timeout=5)


def test_follow_does_not_duplicate_partial_line(project: Path, live_env, wait_for) -> None:
    # Recorder: complete line, then a partial prompt (no \n), sleep so the
    # partial sits there past several follower loop iterations, then complete
    # the partial line and exit. With the duplication bug, the follower would
    # re-emit "Continue? [Y/n] " on every ~1s tick AND again as part of the
    # full line once the \n arrived.
    script = (
        "printf 'first\\n'; "
        "printf 'Continue? [Y/n] '; "
        "sleep 2.5; "
        "printf 'y\\n'"
    )
    rec = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "pdup", "--",
         "sh", "-c", script],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sessions = project / ".live" / "sessions"
        assert wait_for(lambda: sessions.exists() and any(sessions.iterdir()))
        [sess] = list(sessions.iterdir())
        idx = sess / "lines.0000.idx"
        assert wait_for(lambda: idx.exists() and idx.stat().st_size >= 16,
                        timeout=5.0)

        follower = subprocess.Popen(
            [sys.executable, "-m", "live.cli", "tail", "-f", "pdup"],
            cwd=str(project),
            env=live_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, _ = follower.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            follower.kill()
            stdout, _ = follower.communicate()
            pytest.fail("follower did not exit when recorder finished")

        body = stdout.replace("\r", "")
        # Each fragment appears exactly once.
        assert body.count("first\n") == 1, body
        assert body.count("Continue? [Y/n] ") == 1, body
        assert body.count("y\n") == 1, body
    finally:
        if rec.poll() is None:
            rec.kill()
            rec.wait(timeout=5)


def test_follow_clean_exit_on_sigint(project: Path, live_env, wait_for) -> None:
    # A long-running recorder.
    rec = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "long", "--",
         "sh", "-c", "echo starting; sleep 60"],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sessions = project / ".live" / "sessions"
        assert wait_for(lambda: sessions.exists() and any(sessions.iterdir()))
        [sess] = list(sessions.iterdir())
        idx = sess / "lines.0000.idx"
        assert wait_for(lambda: idx.exists() and idx.stat().st_size >= 16,
                        timeout=5.0)

        follower = subprocess.Popen(
            [sys.executable, "-m", "live.cli", "tail", "-f", "long"],
            cwd=str(project),
            env=live_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.5)  # let it settle
        follower.send_signal(signal.SIGINT)
        try:
            stdout, stderr = follower.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            follower.kill()
            stdout, stderr = follower.communicate()
            pytest.fail("follower did not exit on SIGINT")

        # Clean exit on SIGINT: no exit-code trailer (we don't know yet).
        assert "exit-code=" not in stderr
    finally:
        if rec.poll() is None:
            rec.terminate()
            try:
                rec.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rec.kill()
                rec.wait(timeout=5)
