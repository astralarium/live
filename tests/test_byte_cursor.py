"""`live tail -c +K` resumable byte cursor (consumes `next-byte` from trailer)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def test_tail_c_plus_emits_bytes_after_cursor(project: Path, run_live) -> None:
    """Probe next-byte, then `tail -c +K` should emit only bytes after position K."""
    run_live(
        project,
        "run",
        "-n",
        "bc",
        "--",
        "sh",
        "-c",
        "echo aaa; echo bbb; echo ccc",
    )
    # Get full read + trailer next-byte.
    full = run_live(project, "tail", "-vn", "+0", "bc")
    m = re.search(r"next-byte=(\d+)", full.stderr)
    assert m, full.stderr
    total = int(m.group(1))

    # 1-based GNU position: "aaa\r\n" is bytes 1-5, so +6 starts at "bbb".
    out = run_live(project, "tail", "-c", "+6", "bc")
    text = out.stdout.replace("\r", "")
    assert "bbb" in text and "ccc" in text
    assert "aaa" not in text
    # 15 bytes on disk (3 lines * 5); next unread position is 16.
    assert total == 16


def test_tail_c_plus_cursor_at_end_is_empty(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "bc2", "--", "sh", "-c", "echo a")
    full = run_live(project, "tail", "-vn", "+0", "bc2")
    m = re.search(r"next-byte=(\d+)", full.stderr)
    total = int(m.group(1))

    out = run_live(project, "tail", "-c", f"+{total}", "bc2")
    assert out.stdout == ""


def test_tail_c_plus_cursor_ahead_warns(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "bc3", "--", "sh", "-c", "echo a")
    full = run_live(project, "tail", "-vn", "+0", "bc3")
    m = re.search(r"next-byte=(\d+)", full.stderr)
    total = int(m.group(1))

    out = run_live(project, "tail", "-v", "-c", f"+{total + 1000}", "bc3")
    assert out.stdout == ""
    assert f"from-byte={total + 1000}" in out.stderr
    assert f"> next-byte={total}" in out.stderr
    assert "check id" in out.stderr


def test_tail_c_minus_treated_as_count(project: Path, run_live) -> None:
    """`tail -c -K` is a no-op sign — same as `-c K` (last K bytes)."""
    run_live(project, "run", "-n", "bc4", "--", "sh", "-c", "echo aaa; echo bbb")
    a = run_live(project, "tail", "-c", "5", "bc4").stdout
    b = run_live(project, "tail", "-c", "-5", "bc4").stdout
    assert a == b


def test_tail_c_emits_last_k_bytes(project: Path, run_live) -> None:
    """`tail -c K` emits exactly the last K bytes of the on-disk stream."""
    run_live(
        project, "run", "-n", "bc5", "--", "sh", "-c", "echo aaa; echo bbb; echo ccc"
    )
    full = run_live(project, "cat", "bc5", text=False).stdout
    out = run_live(project, "tail", "-c", "5", "bc5", text=False).stdout
    # On-disk: b"aaa\r\nbbb\r\nccc\r\n" = 15 bytes. Last 5 = b"ccc\r\n".
    assert full == b"aaa\r\nbbb\r\nccc\r\n"
    assert out == full[-5:]


def test_tail_c_larger_than_stream_emits_all(project: Path, run_live) -> None:
    """K bigger than the stream returns the whole stream, not an error."""
    run_live(project, "run", "-n", "bc6", "--", "sh", "-c", "echo a")
    full = run_live(project, "cat", "bc6", text=False).stdout
    out = run_live(project, "tail", "-c", "9999", "bc6", text=False).stdout
    assert out == full


def test_ls_json_exposes_first_byte_last_byte(project: Path, run_live) -> None:
    """`ls --json` carries firstByte/lastByte alongside firstLine/lastLine."""
    run_live(project, "run", "-n", "fb", "--", "echo", "hi")
    out = run_live(project, "ls", "-a", "--json")
    info = json.loads(out.stdout.splitlines()[0])
    assert info["firstByte"] == 0
    assert info["lastByte"] > 0
    assert info["lastByte"] >= info["firstByte"]


def test_bytes_since_reports_partial_line(
    project: Path, live_env, run_live, wait_for, wait_for_session
) -> None:
    """Byte-mode (`tail -vc +0`) surfaces the partial-line marker."""
    script = "printf 'complete line\\n'; printf 'partial prompt > '; sleep 10"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "live.cli",
            "run",
            "-n",
            "bc_partial",
            "--",
            "sh",
            "-c",
            script,
        ],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sess_dir = wait_for_session()
        stream = sess_dir / "stream.0000.log"
        idx = sess_dir / "lines.0000.idx"

        def has_partial() -> bool:
            try:
                s = stream.read_bytes()
                i = idx.read_bytes()
            except FileNotFoundError:
                return False
            return len(i) == 40 and b"partial prompt >" in s and not s.endswith(b"\n")

        assert wait_for(has_partial, timeout=8.0), "partial state never appeared"

        out = run_live(project, "tail", "-vc", "+0", "bc_partial")
        assert "complete line" in out.stdout
        assert "partial prompt >" in out.stdout
        assert "live: partial-line bytes=" in out.stderr
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _configure_rotation(project: Path, *, segment_kb: int, max_kb: int) -> None:
    (project / ".live").mkdir(mode=0o700, exist_ok=True)
    (project / ".live" / "config.json").write_text(
        json.dumps({"segmentKb": segment_kb, "maxKb": max_kb})
    )


def test_first_byte_advances_after_rotation(project: Path, run_live) -> None:
    """After retention drops segments, `firstByte` in ls --json reflects the
    lifetime offset where retained data begins, and `lastByte` is the tip."""
    _configure_rotation(project, segment_kb=1, max_kb=2)
    run_live(
        project,
        "run",
        "-n",
        "spam",
        "--",
        "sh",
        "-c",
        "i=0; while [ $i -lt 250 ]; do "
        "printf 'line-number-%04d-with-padding\\n' $i; i=$((i+1)); done",
    )
    out = run_live(project, "ls", "-a", "--json")
    info = json.loads(out.stdout.splitlines()[0])
    assert info["firstByte"] > 0, "expected rotation to advance firstByte"
    assert info["lastByte"] > info["firstByte"], "lastByte must exceed firstByte"


def test_bytes_since_below_floor_warns_and_resumes(project: Path, run_live) -> None:
    """A byte cursor below the floor emits `dropped K bytes (from-byte=0,
    first-byte=F)` where K=F and F matches `ls --json` firstByte; stdout
    resumes at the floor."""
    _configure_rotation(project, segment_kb=1, max_kb=2)
    run_live(
        project,
        "run",
        "-n",
        "drop",
        "--",
        "sh",
        "-c",
        "i=0; while [ $i -lt 250 ]; do "
        "printf 'line-number-%04d-with-padding\\n' $i; i=$((i+1)); done",
    )
    info = json.loads(
        run_live(project, "ls", "-a", "--json", "drop").stdout.splitlines()[0]
    )
    first_byte = info["firstByte"]
    last_byte = info["lastByte"]
    assert first_byte > 0, (
        "rotation must have advanced firstByte for this to be meaningful"
    )

    out = run_live(project, "tail", "-vc", "+1", "drop", text=False)
    stderr = out.stderr.decode()
    m = re.search(r"dropped (\d+) bytes \(from-byte=(\d+), first-byte=(\d+)\)", stderr)
    assert m, f"missing/malformed gap warning: {stderr!r}"
    dropped, from_byte, reported_floor = (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)),
    )
    # Positions are 1-based: the floor offset F is position F+1.
    assert from_byte == 1
    assert reported_floor == first_byte + 1
    assert dropped == first_byte  # bytes dropped == floor offset when starting at 1
    # Resumes at the floor: stdout byte length equals retained range.
    assert len(out.stdout) == last_byte - first_byte


def test_bytes_since_above_floor_resumes_cleanly(project: Path, run_live) -> None:
    """When `+K` lands inside retained data after rotation, output starts at
    that lifetime offset — no `dropped` warning."""
    _configure_rotation(project, segment_kb=1, max_kb=2)
    run_live(
        project,
        "run",
        "-n",
        "resume",
        "--",
        "sh",
        "-c",
        "i=0; while [ $i -lt 250 ]; do "
        "printf 'line-number-%04d-with-padding\\n' $i; i=$((i+1)); done",
    )
    probe = run_live(project, "ls", "-a", "--json", "resume")
    info = json.loads(probe.stdout.splitlines()[0])
    midpoint = (info["firstByte"] + info["lastByte"]) // 2

    out = run_live(project, "tail", "-vc", f"+{midpoint}", "resume")
    assert "dropped" not in out.stderr
    assert len(out.stdout) > 0
