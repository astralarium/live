from __future__ import annotations

from pathlib import Path


SESSIONS_SUBDIR = "sessions"
CONFIG_NAME = "config.json"
STATE_NAME = "state.json"


def live_dir() -> Path:
    """`~/.live`, auto-created. Re-evaluated each call so tests can set $HOME."""
    p = Path.home() / ".live"
    p.mkdir(mode=0o700, exist_ok=True)
    return p


def sessions_dir() -> Path:
    """`~/.live/sessions/`, auto-created."""
    p = live_dir() / SESSIONS_SUBDIR
    p.mkdir(mode=0o700, exist_ok=True)
    return p


def config_path() -> Path:
    return live_dir() / CONFIG_NAME


def state_path() -> Path:
    return live_dir() / STATE_NAME


def name_lock_path() -> Path:
    """Global lock serializing named-run conflict check + session creation."""
    return live_dir() / "name.lock"


def session_dir(session_id: str) -> Path:
    return sessions_dir() / session_id


def within_cwd(session_cwd: str, cwd: Path | None = None) -> bool:
    """True if `session_cwd` is `cwd` or a descendant (symlink-resolved)."""
    base = (cwd or Path.cwd()).resolve()
    try:
        return Path(session_cwd).resolve().is_relative_to(base)
    except (OSError, ValueError):
        return False
