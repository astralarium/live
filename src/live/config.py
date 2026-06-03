from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import CONFIG_NAME, HOME_LIVE, Scope


DEFAULTS = {
    "ttlDays": 7,
    "maxKb": 512,
    "segmentKb": 64,
    "heartbeatSec": 30,
}


_VALIDATORS = {
    "ttlDays": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
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


def _load_layer(path: Path, *, project_layer: bool) -> dict[str, int]:
    """Read one config layer; return {} for missing/malformed.

    Asymmetric error policy: per-project malformed is logged + ignored,
    home malformed warns and falls back to defaults.
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        msg = f"live: malformed config at {path}: {e}"
        if project_layer:
            print(msg, file=sys.stderr)
        else:
            print(f"{msg} — falling back to defaults", file=sys.stderr)
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


def load_config(scope: Scope) -> Config:
    """Layered config: per-`.live/` over home over compiled defaults."""
    HOME_LIVE.mkdir(mode=0o700, exist_ok=True)
    home_cfg = HOME_LIVE / CONFIG_NAME
    if not home_cfg.exists():
        try:
            home_cfg.write_text(json.dumps(DEFAULTS) + "\n")
        except OSError:
            pass
    home = _load_layer(home_cfg, project_layer=False)
    project = (
        _load_layer(scope.live_dir / CONFIG_NAME, project_layer=True)
        if scope.live_dir != HOME_LIVE
        else {}
    )
    merged = {**DEFAULTS, **home, **project}
    return Config(
        ttl_days=int(merged["ttlDays"]),
        max_kb=int(merged["maxKb"]),
        segment_kb=int(merged["segmentKb"]),
        heartbeat_sec=int(merged["heartbeatSec"]),
    )
