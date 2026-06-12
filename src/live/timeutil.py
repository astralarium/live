"""Time parsing and formatting helpers.

Parsers raise `argparse.ArgumentTypeError` so they can be used directly as
argparse `type=` callables with their messages preserved.
"""

from __future__ import annotations

import argparse
import re
import time
from datetime import datetime

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([dhms])\s*$")


def duration_secs(value: str) -> float | None:
    """`7d`/`12h`/`30m`/`60s` -> seconds; None if not a duration."""
    m = _DURATION_RE.match(value)
    if m is None:
        return None
    n = float(m.group(1))
    unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}[m.group(2)]
    return n * unit


def parse_age(value: str) -> float:
    """Return a cutoff in epoch seconds. Sessions exited before it count as older.

    Duration form: `7d`, `12h`, `30m`, `60s` → `now - N`.
    Absolute form: ISO date/datetime (`2026-01-01`, `2026-01-01T12:00:00`); naive
    timestamps are interpreted as local time.
    """
    secs = duration_secs(value)
    if secs is not None:
        return time.time() - secs
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected duration (e.g. 7d, 12h, 30m, 60s) or ISO datetime (got {value!r})"
        )


def parse_time(value: str) -> float:
    """Return a reference time in epoch seconds.

    Epoch form: `1781204738.513` (the trailer's `last-time`).
    Duration form: `7d`, `12h`, `30m`, `60s` → `now - N`.
    Absolute form: ISO date/datetime; naive timestamps are local time.
    """
    secs = duration_secs(value)
    if secs is not None:
        return time.time() - secs
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected epoch seconds, duration (e.g. 30m), "
            f"or ISO datetime (got {value!r})"
        )


def fmt_duration(secs: float) -> str:
    """Compact kubectl-style duration: 45s, 5m12s, 2h30m, 3d4h."""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"
