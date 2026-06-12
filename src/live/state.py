"""Persistent CLI state at `~/.live/state.json`.

Currently holds `lastSweepTime`, used to throttle the lifecycle sweep that
every verb piggybacks (see `session.sweep_all`). Atomic writes via
`os.replace`; concurrent invocations are safe — at worst two of them sweep
once, then both stamp roughly the same timestamp.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from .paths import state_path


SWEEP_INTERVAL_SEC = 3600


def _load() -> dict:
    try:
        with state_path().open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def last_sweep_time() -> float:
    """Seconds-since-epoch of the last recorded sweep; 0.0 if never."""
    val = _load().get("lastSweepTime")
    return float(val) if isinstance(val, (int, float)) else 0.0


def should_sweep(*, now: float | None = None) -> bool:
    """True if `SWEEP_INTERVAL_SEC` has elapsed since the last sweep. A stamp
    in the future (clock stepped backwards) must not disable sweeping."""
    t = now if now is not None else time.time()
    last = last_sweep_time()
    if last > t:
        return True
    return t - last >= SWEEP_INTERVAL_SEC


def mark_swept(now: float | None = None) -> None:
    """Stamp `lastSweepTime` atomically. Failures are swallowed."""
    data = _load()
    data["lastSweepTime"] = now if now is not None else time.time()
    path = state_path()
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".state.",
            suffix=".tmp",
            delete=False,
        ) as f:
            json.dump(data, f, separators=(",", ":"))
            f.write("\n")
            tmp_name = f.name
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(path))
    except OSError:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
