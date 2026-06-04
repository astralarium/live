"""Config loading: missing/malformed/invalid paths fall back to defaults."""

from __future__ import annotations

import json
from pathlib import Path

from live.config import DEFAULTS, _load_file


def test_load_file_returns_empty_for_missing(tmp_path: Path) -> None:
    assert _load_file(tmp_path / "nope.json") == {}


def test_load_file_warns_on_malformed_json(tmp_path: Path, capsys) -> None:
    p = tmp_path / "config.json"
    p.write_text("{not valid json")
    assert _load_file(p) == {}
    assert "malformed config" in capsys.readouterr().err


def test_load_file_ignores_non_dict_top_level(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert _load_file(p) == {}


def test_load_file_drops_invalid_value_types(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"maxKb": "not-an-int", "segmentKb": 64}))
    assert _load_file(p) == {"segmentKb": 64}


def test_load_file_rejects_booleans_as_integers(tmp_path: Path) -> None:
    """`isinstance(True, int)` is True in Python; the validator must reject it."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"maxKb": True, "segmentKb": 32}))
    assert _load_file(p) == {"segmentKb": 32}


def test_load_file_drops_non_positive_max_kb(tmp_path: Path) -> None:
    """maxKb / segmentKb / heartbeatSec require > 0; ttlDays accepts negatives."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"maxKb": 0, "segmentKb": -5, "ttlDays": -1}))
    assert _load_file(p) == {"ttlDays": -1}


def test_load_file_ignores_unknown_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"unknown": 42, "maxKb": 32}))
    assert _load_file(p) == {"maxKb": 32}


def test_defaults_have_every_documented_key() -> None:
    """Guard against drift between DEFAULTS and Config fields."""
    assert set(DEFAULTS) == {"ttlDays", "maxKb", "segmentKb", "heartbeatSec"}
