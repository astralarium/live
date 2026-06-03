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
def live_env() -> dict:
    env = dict(os.environ)
    src = str(SRC)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
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
def project(tmp_path: Path, run_live) -> Path:
    """An initialized .live/ project."""
    run_live(tmp_path, "init")
    return tmp_path
