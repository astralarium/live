"""CLI verbs: run, ls, cat, head, tail, rm, llms.txt, completion."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from .config import Config, load_config
from .lock import kill_pid, probe_held, read_lock_pid
from .reader import (
    ReadResult,
    bytes_since,
    cat_all,
    head_drop_last,
    head_first,
    lines_since,
    lines_since_time,
    lines_until_time,
    should_strip_ansi,
    strip_ansi,
    tail_last,
)
from .recorder import record
from .select_session import SelectorError, resolve_many, resolve_one
from .sweep import SessionInfo, list_sessions, sweep_all

# ----- error helpers -----


def _err(msg: str) -> None:
    print(f"live: {msg}", file=sys.stderr)


def _emit_read_result(
    result: ReadResult,
    info: SessionInfo,
    cfg: Config,
    *,
    verbose: bool,
    strip: bool,
) -> None:
    """Apply ANSI rules, print stdout, emit ordered stderr lines + trailer."""
    out = result.stdout
    if strip:
        out = strip_ansi(out)
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass

    if not verbose:
        return

    # Ordered stderr lines: gap, cursor-ahead handled at call sites,
    # then partial, hung, exit, then trailer.
    for line in result.stderr_lines:
        print(f"live: {line}", file=sys.stderr)
    if result.partial_bytes:
        print(
            f"live: partial-line bytes={result.partial_bytes}"
            f" age={result.partial_age:.3f}",
            file=sys.stderr,
        )
    if info.status == "hung":
        print(
            f"live: status=hung last-activity={info.last_activity:.3f}",
            file=sys.stderr,
        )
    elif info.status == "exited":
        if info.exit_code is not None:
            print(f"live: exit-code={info.exit_code}", file=sys.stderr)
    elif info.status == "inconsistent":
        print("live: exit=inconsistent", file=sys.stderr)
    print(
        f"live: id={info.id} at-line={result.last_line}"
        f" at-time={result.at_time:.3f} at-byte={result.at_byte}",
        file=sys.stderr,
    )


# ----- verbs -----


def _scope_filter(args) -> Path | None:
    """cwd filter for read verbs; None means global view (`-g`)."""
    return None if getattr(args, "global_", False) else Path.cwd()


def cmd_run(args) -> int:
    cfg = load_config()
    sweep_all(cfg)
    cmd = list(args.cmd)
    # argparse.REMAINDER hands us "--" as the first token if the user wrote it
    # to defend a flag-starting command. Strip it so execvp gets the real argv.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        _err("run: missing command")
        return 2
    return record(cfg, cmd, name=args.name)


def cmd_ls(args) -> int:
    cfg = load_config()
    sweep_all(cfg)
    sessions = list_sessions(cfg, cwd_filter=_scope_filter(args))

    if args.selector:
        token = args.selector
        name_matches = [s for s in sessions if s.meta.name == token]
        if name_matches:
            sessions = name_matches
        else:
            sessions = [s for s in sessions if s.id.startswith(token)]

    if not args.all:
        sessions = [s for s in sessions if s.status in ("running", "hung")]

    if args.json:
        for s in sessions:
            obj = {
                "id": s.id,
                "command": s.meta.command,
                "cwd": s.meta.cwd,
                "startedAt": s.meta.started_at,
                "status": s.status,
                "path": str(s.path),
                "firstSegment": s.watermarks.first_segment,
                "lastSegment": s.watermarks.last_segment,
                "firstLine": s.watermarks.first_line,
                "lastLine": s.watermarks.last_line,
                "count": s.watermarks.count,
                "lastActivity": s.last_activity,
            }
            if s.meta.name is not None:
                obj["name"] = s.meta.name
            if s.exited_at is not None:
                obj["exitedAt"] = s.exited_at
            if s.exit_code is not None:
                obj["exitCode"] = s.exit_code
            print(json.dumps(obj, separators=(",", ":")))
        return 0

    # Human columns: header + equal-width fields (last column unpadded).
    if not sessions:
        return 0
    headers = ("ID", "STATUS", "NAME", "COMMAND")
    rows = [
        (s.id[:8], s.status, s.meta.name or "-", " ".join(s.meta.command))
        for s in sessions
    ]
    widths = [
        max(len(headers[i]), max(len(r[i]) for r in rows))
        for i in range(len(headers) - 1)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    return 0


def _get_session_or_fail(
    token: str, cwd_filter: Path | None
) -> tuple[SessionInfo, Config] | None:
    cfg = load_config()
    sweep_all(cfg)
    sessions = list_sessions(cfg, cwd_filter=cwd_filter)
    try:
        info = resolve_one(sessions, token)
    except SelectorError as e:
        _err(str(e))
        return None
    return info, cfg


def cmd_cat(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 2
    info, cfg = res
    result = cat_all(info.path)
    strip = should_strip_ansi(
        explicit_strip=args.strip_ansi,
        explicit_raw=args.raw,
        stdout_is_tty=sys.stdout.isatty(),
    )
    _emit_read_result(result, info, cfg, verbose=args.verbose, strip=strip)
    return 0


def cmd_head(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 2
    info, cfg = res

    # args.lines / args.bytes_ are None or ("count" | "cursor", int).
    n_kind, n_val = args.lines if args.lines is not None else (None, None)
    c_kind, c_val = args.bytes_ if args.bytes_ is not None else (None, None)

    if n_kind == "cursor":
        # GNU `head -n -K`: drop the last K lines.
        result = head_drop_last(info.path, n_lines=n_val)
    elif c_kind == "cursor":
        # GNU `head -c -K`: drop the last K bytes.
        result = head_drop_last(info.path, c_bytes=c_val)
    elif args.time is not None:
        result = lines_until_time(info.path, until_t=args.time)
    else:
        result = head_first(
            info.path,
            n_lines=n_val if n_kind == "count" else None,
            c_bytes=c_val if c_kind == "count" else None,
        )
    strip = should_strip_ansi(
        explicit_strip=args.strip_ansi,
        explicit_raw=args.raw,
        stdout_is_tty=sys.stdout.isatty(),
    )
    _emit_read_result(result, info, cfg, verbose=args.verbose, strip=strip)
    return 0


def cmd_tail(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 2
    info, cfg = res

    # args.lines / args.bytes_ are None or ("count" | "cursor", int).
    n_kind, n_val = args.lines if args.lines is not None else (None, None)
    c_kind, c_val = args.bytes_ if args.bytes_ is not None else (None, None)

    is_line_cursor = n_kind == "cursor"
    is_byte_cursor = c_kind == "cursor"
    is_time_cursor = args.time is not None
    # Mutual exclusivity (-n / -c / -t) is enforced by argparse.

    verbose = args.verbose
    if is_line_cursor:
        result = lines_since(info.path, since=n_val)
        # Caught-up polls (n_val == last_line + 1) are silent. Warn only when
        # the cursor is multiple lines past the session's lastLine.
        if n_val > result.last_line + 1 and result.last_line:
            result.stderr_lines.append(
                f"since={n_val} > at-line={result.last_line}; check id"
            )
    elif is_byte_cursor:
        result = bytes_since(info.path, since=c_val)
        if c_val > result.at_byte and result.at_byte:
            result.stderr_lines.append(
                f"bytes={c_val} > at-byte={result.at_byte}; check id"
            )
    elif is_time_cursor:
        result = lines_since_time(info.path, since_t=args.time)
        if args.time > result.at_time and result.at_time:
            result.stderr_lines.append(
                f"time={args.time:.3f} > at-time={result.at_time:.3f}; check id"
            )
    else:
        result = tail_last(
            info.path,
            n_lines=n_val if n_kind == "count" else None,
            c_bytes=c_val if c_kind == "count" else None,
        )

    strip = should_strip_ansi(
        explicit_strip=args.strip_ansi,
        explicit_raw=args.raw,
        stdout_is_tty=sys.stdout.isatty(),
    )

    if args.follow:
        # Emit the initial slice without verbose trailer, then enter follow mode.
        out = strip_ansi(result.stdout) if strip else result.stdout
        try:
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            return 0
        from .follow import follow_session

        return follow_session(
            cfg=cfg,
            info=info,
            initial_cursor=result.last_line,
            initial_partial_bytes=result.partial_bytes,
            initial_partial_seg=result.partial_seg,
            strip=strip,
        )

    _emit_read_result(result, info, cfg, verbose=verbose, strip=strip)
    return 0


def cmd_rm(args) -> int:
    if not args.selectors and not args.all_exited:
        _err("rm: missing selector (or --all-exited)")
        return 2

    cfg = load_config()
    sweep_all(cfg)
    sessions = list_sessions(cfg, cwd_filter=_scope_filter(args))

    targets: list[SessionInfo] = []
    any_error = False

    if args.all_exited:
        targets.extend([s for s in sessions if s.status in ("exited", "inconsistent")])

    for token in args.selectors or []:
        try:
            targets.extend(resolve_many(sessions, token))
        except SelectorError as e:
            _err(str(e))
            any_error = True

    # Dedupe by id.
    seen: set[str] = set()
    unique: list[SessionInfo] = []
    for s in targets:
        if s.id not in seen:
            seen.add(s.id)
            unique.append(s)

    for s in unique:
        try:
            _delete_session(s, force=args.force)
        except Exception as e:
            _err(f"rm {s.id[:8]}: {e}")
            any_error = True

    return 1 if any_error else 0


def _delete_session(info: SessionInfo, *, force: bool) -> None:
    """Delete a session directory. If running and force=True, kill the recorder."""
    from .format import LOCK_NAME

    lock_path = info.path / LOCK_NAME
    held = probe_held(lock_path)
    if held is True:
        if not force:
            raise RuntimeError(f"session {info.id[:8]} is running (use -f)")
        pid = read_lock_pid(lock_path)
        if pid:
            kill_pid(pid, signal.SIGTERM)
            # Wait up to 5s for flock release.
            deadline = time.time() + 5
            while time.time() < deadline:
                still = probe_held(lock_path)
                if still is not True:
                    break
                time.sleep(0.1)
            else:
                kill_pid(pid, signal.SIGKILL)
                time.sleep(0.2)
    shutil.rmtree(info.path, ignore_errors=True)


LLMS_TXT_PAYLOAD = """\
This project uses `live`, a CLI streamer.

List available sessions:
  live ls [-a] [--json] [<SELECTOR>]

Read output from a live session:
  live tail -vn +<N> <SELECTOR>

<SELECTOR>: UUID prefix or NAME (newest match)
<N>: line number

stdout: command stdout+stderr lines with n>=N
stderr: live verbose output
  trailer: "live: id=<uuid> at-line=<L> at-time=<T> at-byte=<B>"
  stop:    "live: exit-code=" or "live: exit=inconsistent"
  hung:    "live: status=hung last-activity=<s>" (alive, but stalled)
  gap:     "live: dropped <k> lines (since=<N>, first retained=<F>)"
  partial: "live: partial-line bytes=<k> age=<s>"

Begin reading from +0. Continue reading with: next +<N> = <L>+1; reset <N>=0 if <uuid> changes (new session)

Pipe output from `live tail` and `live cat` to tools like `grep`.
"""


def cmd_llms_txt(args) -> int:
    sys.stdout.write(LLMS_TXT_PAYLOAD)
    return 0


def cmd_completion(args) -> int:
    from .completion import script_for

    payload = script_for(args.shell)
    if payload is None:
        _err(f"unknown shell: {args.shell}")
        return 2
    sys.stdout.write(payload)
    return 0
