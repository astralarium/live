"""CLI verbs: init, run, ls, cat, tail, rm, llms.txt, completion."""

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
    cat_all,
    lines_since,
    lines_since_time,
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
        f"live: id={info.id} at-line={result.last_line} at-time={result.at_time:.3f}",
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

    # Human columns.
    if not sessions:
        return 0
    for s in sessions:
        id_prefix = s.id[:8]
        status = s.status
        name = s.meta.name or "-"
        command = " ".join(s.meta.command)
        print(f"{id_prefix}  {status:11s}  {name}  {command}")
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


def cmd_tail(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 2
    info, cfg = res

    # args.lines is None or a (kind, int) tuple from _lines_arg.
    since_n: int | None = None
    n_lines: int | None = None
    if args.lines is not None:
        kind, n = args.lines
        if kind == "since":
            since_n = n
        else:
            n_lines = n

    is_line_cursor = since_n is not None
    is_time_cursor = args.since is not None
    # Mutual exclusivity (-n / -c / --since) is enforced by argparse.

    verbose = args.verbose
    if is_line_cursor:
        result = lines_since(info.path, since=since_n)
        if since_n > result.last_line and result.last_line:
            result.stderr_lines.append(
                f"since={since_n} > at-line={result.last_line}; check id"
            )
    elif is_time_cursor:
        result = lines_since_time(info.path, since_t=args.since)
        if args.since > result.at_time and result.at_time:
            result.stderr_lines.append(
                f"since={args.since:.3f} > at-time={result.at_time:.3f}; check id"
            )
    else:
        result = tail_last(info.path, n_lines=n_lines, c_bytes=args.bytes_)

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

<N>: line number to continue
<SELECTOR>: UUID prefix or NAME (newest match)

stdout: command stdout+stderr lines with n>N
stderr: live verbose output
  trailer: "live: id=<uuid> at-line=<L> at-time=<T>"
  stop:    "live: exit-code=" or "live: exit=inconsistent"
  hung:    "live: status=hung last-activity=<s>" (still alive, just stalled)
  gap:     "live: dropped <k> lines (since=<N>, first retained=<F>)"
  partial: "live: partial-line bytes=<k> age=<s>"

To resume reading: next <N> = <L>; reset <N>=0 if <uuid> changes

Pipe output from `live tail` to other tools like `grep`.
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
