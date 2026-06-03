"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
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
    def _run(cwd: Path, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "live.cli", *args],
            cwd=str(cwd),
            env=live_env,
            capture_output=True,
            text=True,
            check=check,
            **kw,
        )

    return _run


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A working directory that doubles as `$HOME` for the isolated `~/.live/` store."""
    return tmp_path
