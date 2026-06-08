"""All `live: …` stderr lines for verbose mode.

Trailer ordering (when multiple apply): extras (gap / cursor-ahead) → partial
→ hung → exit/inconsistent → trailer. Callers walk this sequence.
"""

from __future__ import annotations

import sys

from .session import STATUS_DEAD, SessionInfo


def _emit(msg: str) -> None:
    print(f"live: {msg}", file=sys.stderr)


def emit_extras(lines: list[str]) -> None:
    """Pre-formatted lines accumulated in `ReadResult.stderr_lines`."""
    for line in lines:
        _emit(line)


def emit_partial(bytes_count: int, age: float) -> None:
    _emit(f"partial-line bytes={bytes_count} age={age:.3f}")


def emit_hung(last_activity: float) -> None:
    _emit(f"status=hung last-activity={last_activity:.3f}")


def emit_exit(info: SessionInfo | None) -> None:
    """`exit=inconsistent` and/or `exit-code=N`; no-op for None / running / hung.
    Both can appear if the recorder wrote meta.json before a sweeper observed
    a torn recording."""
    if info is None:
        return
    if info.status == "inconsistent":
        _emit("exit=inconsistent")
    if info.status in STATUS_DEAD and info.exit_code is not None:
        _emit(f"exit-code={info.exit_code}")


def emit_trailer(
    session_id: str, next_line: int, next_byte: int, last_time: float
) -> None:
    """`next-line`/`next-byte` are resume cursors: plug straight into `tail -n
    +<next-line>` or `tail -c +<next-byte>` to read what's been written since.
    `last-time` is the timestamp of the most recent write."""
    _emit(
        f"id={session_id} next-line={next_line}"
        f" next-byte={next_byte} last-time={last_time:.3f}"
    )
