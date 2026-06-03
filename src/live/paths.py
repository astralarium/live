from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


HOME_LIVE = Path.home() / ".live"
LIVE_DIR_NAME = ".live"
SESSIONS_SUBDIR = "sessions"
GITIGNORE_NAME = ".gitignore"
CONFIG_NAME = "config.json"


@dataclass(frozen=True)
class Scope:
    live_dir: Path

    @property
    def sessions_dir(self) -> Path:
        return self.live_dir / SESSIONS_SUBDIR


def find_live_dir(start: Path | None = None) -> Path | None:
    """Walk up from start (cwd) to find the nearest `.live/` directory.

    Returns the absolute path to the `.live/` directory, or None.
    """
    cur = (start or Path.cwd()).resolve()
    while True:
        candidate = cur / LIVE_DIR_NAME
        if candidate.is_dir():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def resolve_scope(start: Path | None = None, auto_create_home: bool = True) -> Scope:
    """Scope is the nearest walk-up `.live/`, or `~/.live/`.

    Used by every verb; `auto_create_home=True` creates `~/.live/` and its
    `sessions/` subdir if walk-up finds nothing.
    """
    found = find_live_dir(start)
    if found is not None:
        return Scope(found)
    if auto_create_home:
        HOME_LIVE.mkdir(mode=0o700, exist_ok=True)
        (HOME_LIVE / SESSIONS_SUBDIR).mkdir(mode=0o700, exist_ok=True)
    return Scope(HOME_LIVE)


def session_dir(scope: Scope, session_id: str) -> Path:
    return scope.sessions_dir / session_id


def init_project_live_dir(cwd: Path | None = None) -> Path:
    """Create `.live/`, `.live/sessions/`, and `.live/.gitignore` in cwd.

    Idempotent.
    """
    root = (cwd or Path.cwd()).resolve()
    live = root / LIVE_DIR_NAME
    live.mkdir(mode=0o700, exist_ok=True)
    (live / SESSIONS_SUBDIR).mkdir(mode=0o700, exist_ok=True)
    gitignore = live / GITIGNORE_NAME
    if not gitignore.exists():
        gitignore.write_text(f"{SESSIONS_SUBDIR}/\n")
    return live
