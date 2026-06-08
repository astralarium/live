"""On-disk format helpers: meta.json, idx records, segment enumeration.

Index format: 8-byte header (`>Q` lifetime byte offset of segment start), then
append-only 24-byte records (`>Qdq` line number / timestamp / lifetime byte
offset of line's first byte). All integers big-endian; timestamps are seconds
since epoch.
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path


META_NAME = "meta.json"
LOCK_NAME = "process.lock"
DEAD_NAME = "deadAt"
INCONSISTENT_MARKER = b"inconsistent\n"

IDX_HEADER = struct.Struct(">Q")
IDX_HEADER_SIZE = IDX_HEADER.size
IDX_RECORD = struct.Struct(">Qdq")
IDX_RECORD_SIZE = IDX_RECORD.size

_STREAM_RE = re.compile(r"^stream\.(\d+)\.log$")
_IDX_RE = re.compile(r"^lines\.(\d+)\.idx$")


def stream_name(seg: int) -> str:
    return f"stream.{seg:04d}.log"


def idx_name(seg: int) -> str:
    return f"lines.{seg:04d}.idx"


@dataclass(frozen=True)
class Meta:
    """Session metadata. Times are float seconds since epoch."""

    id: str
    command: list[str]
    cwd: str
    started_at: float
    exited_at: float | None = None
    exit_code: int | None = None
    name: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "command": list(self.command),
            "cwd": self.cwd,
            "startedAt": self.started_at,
            "exitedAt": self.exited_at,
            "exitCode": self.exit_code,
        }
        if self.name is not None:
            d["name"] = self.name
        return d

    @staticmethod
    def from_dict(d: dict) -> "Meta":
        return Meta(
            id=d["id"],
            command=list(d["command"]),
            cwd=d["cwd"],
            started_at=float(d["startedAt"]),
            exited_at=float(d["exitedAt"]) if d.get("exitedAt") is not None else None,
            exit_code=d.get("exitCode"),
            name=d.get("name"),
        )


def write_meta_atomic(session_dir: Path, meta: Meta) -> None:
    """Write meta.json atomically (same-filesystem tempfile + fsync + rename)."""
    payload = json.dumps(meta.to_dict(), indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=".meta.", suffix=".tmp", dir=str(session_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(session_dir / META_NAME))
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def read_meta(session_dir: Path) -> Meta | None:
    path = session_dir / META_NAME
    try:
        with path.open("r", encoding="utf-8") as f:
            return Meta.from_dict(json.load(f))
    except (FileNotFoundError, ValueError, KeyError):
        return None


def read_segment_start(idx_path: Path) -> int | None:
    """Lifetime start byte from the idx header. None if the file is missing or
    shorter than the header."""
    try:
        with idx_path.open("rb") as f:
            buf = f.read(IDX_HEADER_SIZE)
            if len(buf) < IDX_HEADER_SIZE:
                return None
            return IDX_HEADER.unpack(buf)[0]
    except FileNotFoundError:
        return None


def segment_tip_byte(idx_path: Path, stream_path: Path) -> int:
    """Lifetime byte offset just past a segment's current contents (header
    start + on-disk stream size). Partial-line bytes included; 0 if missing."""
    start = read_segment_start(idx_path) or 0
    try:
        size = os.path.getsize(stream_path)
    except FileNotFoundError:
        size = 0
    return start + size


def list_segments(session_dir: Path) -> list[int]:
    """Stream segment numbers from `stream.*.log`, sorted ascending."""
    try:
        entries = os.listdir(session_dir)
    except FileNotFoundError:
        return []
    nums: list[int] = []
    for name in entries:
        m = _STREAM_RE.match(name)
        if m:
            nums.append(int(m.group(1)))
    nums.sort()
    return nums


def count_complete_lines(stream_path: Path) -> int:
    count = 0
    try:
        with stream_path.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                count += chunk.count(b"\n")
    except FileNotFoundError:
        return 0
    return count


def idx_record_count(idx_path: Path) -> int:
    try:
        size = os.path.getsize(idx_path)
    except FileNotFoundError:
        return 0
    if size < IDX_HEADER_SIZE:
        return 0
    return (size - IDX_HEADER_SIZE) // IDX_RECORD_SIZE


def first_idx_record(idx_path: Path) -> tuple[int, float, int] | None:
    """Return the first record as (line, timestamp, byte offset). None if no
    records exist yet (header-only or missing file)."""
    try:
        with idx_path.open("rb") as f:
            f.seek(IDX_HEADER_SIZE)
            buf = f.read(IDX_RECORD_SIZE)
            if len(buf) < IDX_RECORD_SIZE:
                return None
            return IDX_RECORD.unpack(buf)
    except FileNotFoundError:
        return None


def last_idx_record(idx_path: Path) -> tuple[int, float, int] | None:
    """Return the trailing record as (line, timestamp, byte offset). None if no
    records exist yet (header-only or missing file)."""
    try:
        size = os.path.getsize(idx_path)
        if size < IDX_HEADER_SIZE + IDX_RECORD_SIZE:
            return None
        with idx_path.open("rb") as f:
            f.seek(size - IDX_RECORD_SIZE)
            buf = f.read(IDX_RECORD_SIZE)
        return IDX_RECORD.unpack(buf)
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Watermarks:
    first_segment: int
    last_segment: int
    first_line: int  # 0 if no records
    last_line: int  # 0 if no records
    first_byte: int  # 0 if no segments
    last_byte: int  # 0 if no segments
    count: int  # last - first + 1, or 0


def compute_watermarks(session_dir: Path) -> Watermarks:
    segs = list_segments(session_dir)
    if not segs:
        return Watermarks(0, 0, 0, 0, 0, 0, 0)

    first_byte = read_segment_start(session_dir / idx_name(segs[0])) or 0

    first_n = 0
    for seg in segs:
        rec = first_idx_record(session_dir / idx_name(seg))
        if rec is not None:
            first_n = rec[0]
            break

    last_n = 0
    for seg in reversed(segs):
        rec = last_idx_record(session_dir / idx_name(seg))
        if rec is not None:
            last_n = rec[0]
            break

    last_byte = segment_tip_byte(
        session_dir / idx_name(segs[-1]),
        session_dir / stream_name(segs[-1]),
    )

    count = last_n - first_n + 1 if last_n else 0
    return Watermarks(segs[0], segs[-1], first_n, last_n, first_byte, last_byte, count)


def read_idx_records(idx_path: Path) -> list[tuple[int, float, int]]:
    """Read all (n, t, byte_offset) records from an idx file (header skipped)."""
    try:
        data = idx_path.read_bytes()
    except FileNotFoundError:
        return []
    if len(data) < IDX_HEADER_SIZE:
        return []
    out: list[tuple[int, float, int]] = []
    for i in range(
        IDX_HEADER_SIZE, len(data) - IDX_RECORD_SIZE + 1, IDX_RECORD_SIZE
    ):
        out.append(IDX_RECORD.unpack_from(data, i))
    return out
