"""CLI verbs: run, ls, cat, head, tail, rm, llms.txt, completion."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime
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
from .session import (
    STATUS_DEAD,
    NoSuchSelectorError,
    SelectorError,
    SessionInfo,
    list_sessions,
    resolve_many,
    resolve_one,
    sweep_all,
)
from .verbose import (
    emit_exit,
    emit_extras,
    emit_hung,
    emit_partial,
    emit_trailer,
)

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

    emit_extras(result.stderr_lines)
    if result.partial_bytes:
        emit_partial(result.partial_bytes, result.partial_age)
    if info.status == "hung":
        emit_hung(info.last_activity)
    else:
        emit_exit(info)
    emit_trailer(info.id, result.last_line, result.at_time, result.at_byte)


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
    scope = _scope_filter(args)
    headers = ("ID", "TIME", "STATUS", "NAME", "CWD", "COMMAND")
    rows = [
        (
            s.id[:8],
            _fmt_time(s.meta.started_at),
            s.status,
            s.meta.name or "-",
            _cwd_display(s.meta.cwd, scope),
            " ".join(s.meta.command),
        )
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


def _fmt_time(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _cwd_display(meta_cwd: str, scope: Path | None) -> str:
    """Relative path from `scope` when filtered; absolute `meta_cwd` under -g."""
    if scope is None:
        return meta_cwd
    try:
        return os.path.relpath(Path(meta_cwd).resolve(), scope.resolve())
    except (OSError, ValueError):
        return meta_cwd


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
    filter_exited = args.exited or args.untitled
    match_all = args.all_ or (filter_exited and not args.selectors)

    if not args.selectors and not match_all:
        _err(
            "rm: missing selector (use NAME, UUID-prefix, --exited, --untitled, or --all)"
        )
        return 2

    cfg = load_config()
    sweep_all(cfg)
    sessions = list_sessions(cfg, cwd_filter=_scope_filter(args))

    base: list[SessionInfo] = []
    any_error = False

    if match_all:
        base.extend(sessions)

    for token in args.selectors or []:
        try:
            base.extend(resolve_many(sessions, token))
        except NoSuchSelectorError as e:
            if args.force:
                continue
            _err(str(e))
            any_error = True
        except SelectorError as e:
            _err(str(e))
            any_error = True

    # Dedupe base by id.
    seen: set[str] = set()
    targets: list[SessionInfo] = []
    for s in base:
        if s.id not in seen:
            seen.add(s.id)
            targets.append(s)

    # Filters intersect the base set.
    if filter_exited:
        targets = [s for s in targets if s.status in STATUS_DEAD]
    if args.untitled:
        targets = [s for s in targets if s.meta.name is None]
    if args.older_than is not None:
        targets = [
            s
            for s in targets
            if s.exited_at is not None and s.exited_at < args.older_than
        ]

    for s in targets:
        try:
            _delete_session(s, force=args.force)
        except Exception as e:
            _err(f"rm {s.id[:8]}: {e}")
            any_error = True
            continue
        print(s.id)

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

<SELECTOR>: UUID prefix or NAME (newest match)

Read output from a session:
  live cat -v <SELECTOR>
  live head -v <SELECTOR>

stdout: command stdout+stderr (merged)

stderr: live verbose output (-v):
  trailer: "live: id=<uuid> at-line=<L> at-time=<T> at-byte=<B>"
  stop:    "live: exit-code=" or "live: exit=inconsistent"
  hung:    "live: status=hung last-activity=<s>" (alive, but stalled)
  gap:     "live: dropped <k> lines (since=<N>, first retained=<F>)"
  partial: "live: partial-line bytes=<k> age=<s>"

Continue reading a session:
  live tail -vn +<N> <SELECTOR>

with +<N> = <L>+1
reset <N>=0 if <uuid> changes (new session)
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


def cmd_update_shell(args) -> int:
    from .completion import script_for

    shell = args.shell or _detect_shell()
    if shell is None:
        _err("could not detect shell; pass bash, zsh, or fish")
        return 2
    payload = script_for(shell)
    if payload is None:
        _err(f"unsupported shell: {shell}")
        return 2

    dst, hint = _completion_install_path(shell)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(payload)
    print(f"installed {shell} completion -> {dst}")
    if hint:
        print(hint)
    return 0


def _detect_shell() -> str | None:
    name = os.path.basename(os.environ.get("SHELL", ""))
    if name in ("bash", "zsh", "fish"):
        return name
    try:
        import subprocess

        out = subprocess.run(
            ["ps", "-p", str(os.getppid()), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            name = os.path.basename(out.stdout.strip().lstrip("-"))
            if name in ("bash", "zsh", "fish"):
                return name
    except Exception:
        pass
    return None


def _completion_install_path(shell: str) -> tuple[Path, str | None]:
    """Return (destination, hint). `hint` is a one-line note printed after install."""
    home = Path.home()
    if shell == "bash":
        return home / ".local/share/bash-completion/completions/live", None
    if shell == "fish":
        return home / ".config/fish/completions/live.fish", None
    # zsh — find a writable dir already on $fpath.
    fpath_dirs = _zsh_fpath_dirs()
    for d in fpath_dirs:
        try:
            if d.is_dir() and os.access(d, os.W_OK):
                return d / "_live", None
        except OSError:
            continue
    target_dir = home / ".local/share/zsh/site-functions"
    target = target_dir / "_live"
    return (
        target,
        f"this dir is not on $fpath. add to ~/.zshrc before compinit: fpath=({target_dir} $fpath)",
    )


def _zsh_fpath_dirs() -> list[Path]:
    """Best-effort enumeration of zsh's $fpath.

    $FPATH (scalar twin of $fpath) is NOT exported by default, so we fall back
    to `zsh -ic 'print -rl -- $fpath'` to read the user's interactive setup.
    """
    raw = os.environ.get("FPATH", "")
    if raw:
        return [Path(p) for p in raw.split(":") if p]
    try:
        import subprocess

        out = subprocess.run(
            ["zsh", "-ic", "print -rl -- $fpath"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0:
            return [Path(ln) for ln in out.stdout.splitlines() if ln.startswith("/")]
    except Exception:
        pass
    return []
