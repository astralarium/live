"""`live llms.txt` — agent-facing guide. Pin the schema tokens agents consume."""

from __future__ import annotations

from pathlib import Path


# Tokens agents key off of when parsing `live tail -v` output.
TRAILER_TOKENS = (
    "at-line=",
    "at-time=",
    "at-byte=",
    "exit-code=",
    "exit=inconsistent",
    "status=hung",
    "last-activity=",
    "dropped",
    "first retained=",
    "partial-line",
)


def test_llms_txt_prints_agent_guide_schema(project: Path, run_live) -> None:
    out = run_live(project, "llms.txt")
    assert out.returncode == 0
    body = out.stdout
    assert body, "llms.txt printed nothing"
    for token in TRAILER_TOKENS:
        assert token in body, f"missing token: {token!r}"
    # Spot-check the resume protocol so a careless rewording can't drop it.
    assert "live tail" in body
    assert "+<N>" in body
    assert "SELECTOR" in body
