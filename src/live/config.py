from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .paths import config_path


class ConfigError(Exception):
    """Config file exists but is unusable; message is user-facing (no `live: `)."""


DEFAULTS = {
    "ttlDays": 7,
    "maxKb": 512,
    "segmentKb": 64,
    "heartbeatSec": 30,
}


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


_FIELDS = {
    # name: (validator, expected-value description)
    # ttlDays: any int; negative disables sweeping (keep sessions forever).
    "ttlDays": (_int, "an integer"),
    "maxKb": (lambda v: _int(v) and v > 0, "a positive integer"),
    "segmentKb": (lambda v: _int(v) and v > 0, "a positive integer"),
    "heartbeatSec": (lambda v: _int(v) and v > 0, "a positive integer"),
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


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _load_file(path: Path) -> dict[str, int]:
    """Read config; return {} if missing. Raise ConfigError if unusable."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(
            f"invalid config {path}: unreadable ({e.strerror or e}) — "
            f"fix permissions, or delete the file to regenerate defaults"
        ) from e
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"invalid config {path}: malformed JSON: {e.msg} "
            f"(line {e.lineno} column {e.colno}) — "
            f"fix the file, or delete it to regenerate defaults"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"invalid config {path}: top level expects a JSON object, "
            f"got {_json_type(raw)} — "
            f"fix the file, or delete it to regenerate defaults"
        )
    out: dict[str, int] = {}
    for key, val in raw.items():
        field = _FIELDS.get(key)
        if field is None:
            continue  # unknown keys ignored for forward compatibility
        validate, expected = field
        if not validate(val):
            raise ConfigError(
                f"invalid config {path}: {key} expects {expected}, "
                f"got {json.dumps(val)} — "
                f"fix the field, or delete the file to regenerate defaults"
            )
        out[key] = val
    return out


def load_config() -> Config:
    """Read `~/.live/config.json`, auto-creating it with compiled defaults.

    Missing known fields fall back to defaults (partial files are valid).
    Raises ConfigError if the file is unreadable, malformed, or a known
    field has an invalid value.
    """
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
