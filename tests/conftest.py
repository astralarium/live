"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"


@pytest.fixture
def live_env(tmp_path: Path) -> dict:
    """Env for `live` subprocesses with $HOME pointed at tmp_path.

    `Path.home()` (and therefore `~/.live/`) resolves to `tmp_path/.live/`,
    isolating the test from the user's real session store.
    """
    env = dict(os.environ)
    src = str(SRC)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    env["HOME"] = str(tmp_path)
    return env


@pytest.fixture
def run_live(live_env):
    """Run `live <args>` and capture output. Pass `text=False` for raw bytes."""
    def _run(
        cwd: Path,
        *args: str,
        check: bool = True,
        text: bool = True,
        **kw,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "live.cli", *args],
            cwd=str(cwd),
            env=live_env,
            capture_output=True,
            text=text,
            check=check,
            **kw,
        )

    return _run


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A working directory that doubles as `$HOME` for the isolated `~/.live/` store."""
    return tmp_path


@pytest.fixture
def wait_for():
    """Poll `predicate` until it returns truthy or timeout elapses."""
    def _impl(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
        deadline = time.time() + interval + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    return _impl


@pytest.fixture
def wait_for_session(project: Path, wait_for):
    """Block until exactly one session directory appears under `~/.live/sessions/`.

    Returns the session directory `Path`. Asserts on timeout.
    """
    def _impl(timeout: float = 5.0) -> Path:
        sessions = project / ".live" / "sessions"
        assert wait_for(
            lambda: sessions.exists() and any(sessions.iterdir()),
            timeout=timeout,
        ), "no session directory appeared"
        [d] = list(sessions.iterdir())
        return d

    return _impl


@pytest.fixture
def spawn_run(project: Path, live_env):
    """Spawn `live run [extra_args] -- sh -c 'echo go; sleep 60'` in the background.

    Returns a callable that yields the `Popen`. Background recorders are
    SIGTERM'd (then SIGKILL'd) at teardown if still alive.
    """
    procs: list[subprocess.Popen] = []

    def _spawn(*extra_args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "live.cli", "run", *extra_args, "--",
             "sh", "-c", "echo go; sleep 60"],
            cwd=str(project),
            env=live_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    yield _spawn

    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)


@pytest.fixture
def live_shim(tmp_path: Path, live_env):
    """Install a `live` shim on `$PATH` that re-execs `python -m live.cli`.

    Needed when a test drives a completion script that itself shells out to
    `live completion ...`. Returns the env dict (a copy of `live_env`) with
    `PATH` prepended.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "live"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} -m live.cli "$@"\n')
    shim.chmod(0o755)
    env = dict(live_env)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env
