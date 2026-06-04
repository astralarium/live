from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import config_path


DEFAULTS = {
    "ttlDays": 7,
    "maxKb": 512,
    "segmentKb": 64,
    "heartbeatSec": 30,
}


_VALIDATORS = {
    # ttlDays: any int; negative disables sweeping (keep sessions forever).
    "ttlDays": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "maxKb": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
    "segmentKb": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
    "heartbeatSec": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
}


@dataclass(frozen=True)
class Config:
    ttl_days: int
    max_kb: int
    segment_kb: int
    heartbeat_sec: int

    @property
    def max_bytes(self) -> int:
        return self.max_kb * 1024

    @property
    def segment_bytes(self) -> int:
        return self.segment_kb * 1024


def _load_file(path: Path) -> dict[str, int]:
    """Read config; return {} for missing/malformed (warns on malformed)."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        print(f"live: malformed config at {path}: {e} — falling back to defaults",
              file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in raw.items():
        validator = _VALIDATORS.get(key)
        if validator is None:
            continue
        if validator(val):
            out[key] = val
    return out


def load_config() -> Config:
    """Read `~/.live/config.json`, falling back per-field to compiled defaults."""
    cfg_path = config_path()
    if not cfg_path.exists():
        try:
            cfg_path.write_text(json.dumps(DEFAULTS) + "\n")
        except OSError:
            pass
    merged = {**DEFAULTS, **_load_file(cfg_path)}
    return Config(
        ttl_days=int(merged["ttlDays"]),
        max_kb=int(merged["maxKb"]),
        segment_kb=int(merged["segmentKb"]),
        heartbeat_sec=int(merged["heartbeatSec"]),
    )
