"""Config loading: a bad config file fails hard; missing file auto-creates defaults."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from live.config import DEFAULTS, ConfigError, _load_file, load_config


# ----- unit: _load_file -----


def test_load_file_returns_empty_for_missing(tmp_path: Path) -> None:
    assert _load_file(tmp_path / "nope.json") == {}


def test_load_file_raises_on_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{not valid json")
    with pytest.raises(ConfigError) as exc:
        _load_file(p)
    msg = str(exc.value)
    assert f"invalid config {p}" in msg
    assert "malformed JSON" in msg
    assert "line 1" in msg  # parse error position
    assert "delete" in msg


def test_load_file_raises_on_non_dict_top_level(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ConfigError) as exc:
        _load_file(p)
    msg = str(exc.value)
    assert "expects a JSON object" in msg
    assert "got an array" in msg


@pytest.mark.parametrize(
    ("field", "bad", "expected"),
    [
        ("ttlDays", "7d", "an integer"),
        ("ttlDays", 1.5, "an integer"),
        ("maxKb", "512K", "a positive integer"),
        ("maxKb", 0, "a positive integer"),
        ("segmentKb", -5, "a positive integer"),
        ("segmentKb", None, "a positive integer"),
        ("heartbeatSec", [30], "a positive integer"),
    ],
)
def test_load_file_raises_on_invalid_field(
    tmp_path: Path, field: str, bad, expected: str
) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({field: bad}))
    with pytest.raises(ConfigError) as exc:
        _load_file(p)
    msg = str(exc.value)
    assert f"invalid config {p}" in msg
    assert f"{field} expects {expected}" in msg
    assert f"got {json.dumps(bad)}" in msg
    assert "delete the file to regenerate defaults" in msg


def test_load_file_rejects_booleans_as_integers(tmp_path: Path) -> None:
    """`isinstance(True, int)` is True in Python; the validator must reject it."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"maxKb": True}))
    with pytest.raises(ConfigError, match="maxKb expects a positive integer"):
        _load_file(p)


def test_load_file_ignores_unknown_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"unknown": 42, "maxKb": 32}))
    assert _load_file(p) == {"maxKb": 32}


def test_defaults_have_every_documented_key() -> None:
    """Guard against drift between DEFAULTS and Config fields."""
    assert set(DEFAULTS) == {"ttlDays", "maxKb", "segmentKb", "heartbeatSec"}


# ----- unit: load_config -----


def test_load_config_auto_creates_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.max_kb == DEFAULTS["maxKb"]
    written = json.loads((tmp_path / ".live" / "config.json").read_text())
    assert written == DEFAULTS


def test_load_config_partial_file_merges_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    live = tmp_path / ".live"
    live.mkdir()
    (live / "config.json").write_text(json.dumps({"maxKb": 64}))
    cfg = load_config()
    assert cfg.max_kb == 64
    assert cfg.ttl_days == DEFAULTS["ttlDays"]
    assert cfg.segment_kb == DEFAULTS["segmentKb"]
    assert cfg.heartbeat_sec == DEFAULTS["heartbeatSec"]


def test_load_config_raises_on_invalid_field(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    live = tmp_path / ".live"
    live.mkdir()
    (live / "config.json").write_text(json.dumps({"heartbeatSec": "30s"}))
    with pytest.raises(ConfigError, match="heartbeatSec expects a positive integer"):
        load_config()


# ----- end-to-end: bad config fails every verb, help still works -----


def _write_config(home: Path, text: str) -> Path:
    live = home / ".live"
    live.mkdir(exist_ok=True)
    p = live / "config.json"
    p.write_text(text)
    return p


def test_cli_fails_hard_on_invalid_field(project: Path, run_live) -> None:
    p = _write_config(project, json.dumps({"maxKb": "512K"}))
    res = run_live(project, "ps", check=False)
    assert res.returncode == 1
    assert res.stderr.startswith("live: invalid config")
    assert str(p) in res.stderr
    assert 'maxKb expects a positive integer, got "512K"' in res.stderr
    assert "delete the file to regenerate defaults" in res.stderr


def test_cli_fails_hard_on_malformed_json(project: Path, run_live) -> None:
    p = _write_config(project, "{not valid json")
    res = run_live(project, "ps", check=False)
    assert res.returncode == 1
    assert res.stderr.startswith("live: invalid config")
    assert str(p) in res.stderr
    assert "malformed JSON" in res.stderr
    assert "line 1" in res.stderr


def test_cli_help_and_version_skip_config(project: Path, run_live) -> None:
    """`live`, `-h`, `--version`, and subcommand `-h` never load config."""
    _write_config(project, "{not valid json")
    for args in ([], ["-h"], ["--version"], ["ps", "-h"]):
        res = run_live(project, *args)
        assert res.returncode == 0
        assert "invalid config" not in res.stderr


def test_cli_unknown_keys_ignored(project: Path, run_live) -> None:
    _write_config(project, json.dumps({"futureKnob": True, "maxKb": 32}))
    res = run_live(project, "ps")
    assert res.returncode == 0
    assert res.stderr == ""
