"""CLI verbs: run, ls, cat, head, tail, rm, llms.txt, completion."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from .ansi import strip_ansi
from .config import Config, load_config
from .format import LOCK_NAME
from .lock import HeldLock, LockTimeout, kill_pid, probe_held, read_lock_pid
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
    tail_last,
)
from .paths import name_lock_path, within_cwd
from .timeutil import fmt_duration
from .recorder import STOP_KILL_DEADLINE_SEC, record, record_detached
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
    emit_trailer(info.id, result.last_line + 1, result.next_byte, result.last_time)


# ----- verbs -----


def _effective_cwd(args) -> Path:
    """The `-C` value, else the caller's cwd."""
    return args.cwd if args.cwd is not None else Path.cwd()


def _scope_filter(args) -> Path | None:
    """cwd filter for read verbs; `-C` overrides, None means global view (`-g`)."""
    return None if args.global_ else _effective_cwd(args)


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
    if args.cwd is not None and not args.cwd.is_dir():
        _err(f"run: no such directory: {args.cwd}")
        return 2
    cwd = _effective_cwd(args)

    # Named runs serialize on a global lock spanning the conflict check and
    # session creation (meta is written before release), so concurrent
    # `run -n NAME` can't both pass the check or hide mid-startup.
    guard = None
    if args.name is not None:
        try:
            guard = HeldLock(name_lock_path())
        except LockTimeout as e:
            _err(f"run: {e}")
            return 1
    try:
        if args.name is not None:
            # Conflict when either cwd contains the other — i.e. some scope
            # would see both sessions under one name. Siblings/disjoint dirs
            # may share it. Only in-scope runs get the stop hint: an
            # ancestor's run is out of scope here, and a child shouldn't be
            # told how to kill its parent.
            in_scope: list[SessionInfo] = []
            ancestors: list[SessionInfo] = []
            for s in list_sessions(cfg):
                if s.meta.name != args.name or s.status in STATUS_DEAD:
                    continue
                if within_cwd(s.meta.cwd, cwd):
                    in_scope.append(s)
                elif within_cwd(str(cwd), Path(s.meta.cwd)):
                    ancestors.append(s)
            if in_scope:
                _err(
                    f"run: session '{args.name}' is already running "
                    f"(id {in_scope[0].id[:8]}); run `live stop {args.name}` first"
                )
                return 1
            if ancestors:
                _err(
                    f"run: session '{args.name}' is already running "
                    f"in ancestor {ancestors[0].meta.cwd} (id {ancestors[0].id[:8]})"
                )
                return 1

        if args.detach:
            session_id, error = record_detached(
                cfg,
                cmd,
                name=args.name,
                geometry=args.geometry,
                cwd=cwd,
                # The recorder must not hold a copy of the name-lock fd: if
                # this CLI dies before its release, that copy would keep the
                # lock held for the session's whole lifetime.
                after_fork=guard.close_inherited if guard is not None else None,
            )
            if error is not None:
                _err(f"run: {error}")
                return 1
            print(session_id)
            return 0

        return record(
            cfg,
            cmd,
            name=args.name,
            geometry=args.geometry,
            cwd=cwd,
            after_setup=guard.release if guard is not None else None,
        )
    finally:
        if guard is not None:
            guard.release()


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
        # Key order mirrors the human columns: identity, status/timing,
        # cwd/command, then storage internals.
        for s in sessions:
            obj = {"id": s.id}
            if s.meta.name is not None:
                obj["name"] = s.meta.name
            obj["status"] = s.status
            obj["startedAt"] = s.meta.started_at
            if s.exited_at is not None:
                obj["exitedAt"] = s.exited_at
            if s.exit_code is not None:
                obj["exitCode"] = s.exit_code
            if s.meta.tty_closed_at is not None:
                obj["ttyClosedAt"] = s.meta.tty_closed_at
            if s.meta.detached:
                obj["detached"] = True
            obj.update(
                {
                    "lastActivity": s.last_activity,
                    "cwd": s.meta.cwd,
                    "command": s.meta.command,
                    "path": str(s.path),
                    "firstLine": s.watermarks.first_line,
                    "lastLine": s.watermarks.last_line,
                    "firstByte": s.watermarks.first_byte,
                    "lastByte": s.watermarks.last_byte,
                    "count": s.watermarks.count,
                }
            )
            print(json.dumps(obj, separators=(",", ":")))
        return 0

    # Human columns: header + equal-width fields (last column unpadded).
    scope = _scope_filter(args)
    now = time.time()
    headers = ("ID", "NAME", "STATUS", "CWD", "COMMAND")
    rows = [
        (
            s.id[:8],
            s.meta.name or "-",
            _fmt_status(s, now),
            _cwd_display(s.meta.cwd, scope),
            " ".join(s.meta.command),
        )
        for s in sessions
    ]
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(len(headers) - 1)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    return 0


def _fmt_status(s: SessionInfo, now: float) -> str:
    """docker-ps style status: Running 5m / Running 5m (hung) / Running 5m
    (tty closed) / Exited (0) 2h ago [detached] / Dead."""
    if s.status in ("running", "hung"):
        up = f"Running {fmt_duration(now - s.meta.started_at)}"
        if s.status == "hung":
            return f"{up} (hung)"
        if s.meta.tty_closed_at is not None:
            return f"{up} (tty closed)"
        return up
    ago = f" {fmt_duration(now - s.exited_at)} ago" if s.exited_at else ""
    tag = " [detached]" if s.meta.detached else ""
    if s.status == "exited":
        code = f" ({s.exit_code})" if s.exit_code is not None else ""
        return f"Exited{code}{ago}{tag}"
    return f"Dead{ago}{tag}"  # inconsistent exit records


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
        return 1
    info, cfg = res
    result = cat_all(info.path)
    strip = should_strip_ansi(
        explicit_strip=args.strip_ansi,
        explicit_raw=args.raw,
        stdout_is_tty=sys.stdout.isatty(),
    )
    _emit_read_result(result, info, verbose=args.verbose, strip=strip)
    return 0


def cmd_head(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 1
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
    _emit_read_result(result, info, verbose=args.verbose, strip=strip)
    return 0


def cmd_tail(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 1
    info, cfg = res

    # args.lines / args.bytes_ are None or ("count" | "cursor", int).
    n_kind, n_val = args.lines if args.lines is not None else (None, None)
    c_kind, c_val = args.bytes_ if args.bytes_ is not None else (None, None)

    is_line_cursor = n_kind == "cursor"
    is_byte_cursor = c_kind == "cursor"
    is_time_cursor = args.time is not None
    # Mutual exclusivity (-n / -c / -t) is enforced by argparse.

    if is_line_cursor:
        result = lines_since(info.path, from_line=n_val)
        # Caught-up polls (n_val == next-line) are silent. Warn only when the
        # cursor is past the session's tip — empty sessions included.
        if n_val > result.last_line + 1:
            result.stderr_lines.append(
                f"from-line={n_val} > next-line={result.last_line + 1}; check id"
            )
    elif is_byte_cursor:
        result = bytes_since(info.path, from_byte=c_val)
        if c_val > result.next_byte:
            result.stderr_lines.append(
                f"from-byte={c_val} > next-byte={result.next_byte}; check id"
            )
    elif is_time_cursor:
        result = lines_since_time(info.path, from_time=args.time)
        if args.time > result.last_time:
            result.stderr_lines.append(
                f"from-time={args.time:.3f} > last-time={result.last_time:.3f}; check id"
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
            initial_byte=result.emitted_byte,
            strip=strip,
            verbose=args.verbose,
        )

    _emit_read_result(result, info, verbose=args.verbose, strip=strip)
    return 0


def cmd_less(args) -> int:
    res = _get_session_or_fail(args.selector, _scope_filter(args))
    if res is None:
        return 1
    info, cfg = res
    strip = should_strip_ansi(
        explicit_strip=args.strip_ansi,
        explicit_raw=args.raw,
        stdout_is_tty=sys.stdout.isatty(),
    )
    from .pager import run_pager

    return run_pager(info, cfg, strip=strip)


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


def cmd_stop(args) -> int:
    if not args.selectors and not args.all_:
        _err("stop: missing selector (use NAME, UUID-prefix, or --all)")
        return 2

    cfg = load_config()
    sweep_all(cfg)
    sessions = list_sessions(cfg, cwd_filter=_scope_filter(args))

    base: list[SessionInfo] = []
    any_error = False

    if args.all_:
        base.extend(s for s in sessions if s.status not in STATUS_DEAD)

    for token in args.selectors or []:
        try:
            matches = resolve_many(sessions, token)
        except SelectorError as e:
            _err(str(e))
            any_error = True
            continue
        running = [s for s in matches if s.status not in STATUS_DEAD]
        if not running:
            _err(f"stop: {token}: not running")
            any_error = True
            continue
        base.extend(running)

    seen: set[str] = set()
    targets: list[SessionInfo] = []
    for s in base:
        if s.id not in seen:
            seen.add(s.id)
            targets.append(s)
    try:
        failed = _stop_recorders(targets)
    except Exception as e:
        _err(f"stop: {e}")
        return 1
    failed_ids = {s.id for s in failed}
    for s in failed:
        _err(f"stop {s.id[:8]}: could not signal recorder")
    for s in targets:
        if s.id not in failed_ids:
            print(s.id)

    return 1 if any_error or failed else 0


def _stop_recorders(infos: list[SessionInfo]) -> list[SessionInfo]:
    """SIGTERM each recorder; SIGKILL any whose flock isn't released within
    STOP_KILL_DEADLINE_SEC. One shared deadline, so N stuck sessions cost
    one wait, not N. Returns sessions whose recorder could not be signaled
    (e.g. another user's process)."""
    pending: list[tuple[Path, int]] = []
    failed: list[SessionInfo] = []
    for info in infos:
        lock_path = info.path / LOCK_NAME
        # Re-probe liveness now: the caller's status snapshot may be stale,
        # and an exited recorder's lock file keeps its (possibly recycled)
        # pid.
        if probe_held(lock_path) is not True:
            continue
        pid = read_lock_pid(lock_path)
        if not pid:
            continue
        if not kill_pid(pid, signal.SIGTERM):
            failed.append(info)
            continue
        pending.append((lock_path, pid))

    deadline = time.time() + STOP_KILL_DEADLINE_SEC
    while pending:
        pending = [lp for lp in pending if probe_held(lp[0]) is True]
        if not pending or time.time() >= deadline:
            break
        time.sleep(0.1)
    for _, pid in pending:
        kill_pid(pid, signal.SIGKILL)
    if pending:
        time.sleep(0.2)
    return failed


def _delete_session(info: SessionInfo, *, force: bool) -> None:
    """Delete a session directory. If running and force=True, kill the recorder."""
    lock_path = info.path / LOCK_NAME
    if probe_held(lock_path) is True:
        if not force:
            raise RuntimeError(f"session {info.id[:8]} is running (use -f)")
        if _stop_recorders([info]):
            # Never delete under a still-live writer we couldn't stop.
            raise RuntimeError("could not signal recorder")
    shutil.rmtree(info.path, ignore_errors=True)


LLMS_TXT_PAYLOAD = """\
This project uses `live`, a CLI streamer.
See live-cmd skill for detailed usage.

Run detached (survives shell exit; prints session UUID):
  live run -dn NAME -- <cmd>

Stop a running session:
  live stop <SELECTOR>

List sessions:
  live ls [-a] [--json] [<SELECTOR>]

<SELECTOR>: UUID prefix or NAME (newest match)

Read output:
  live cat -v <SELECTOR>
  live head -v <SELECTOR>

stdout: merged stdout+stderr logs

stderr: `live` verbose output (-v):
- trailer:
  "live: id=<uuid> next-line=<N> next-byte=<B> last-time=<T>"
- stop: session is done
  "live: exit-code=<code>" or "live: exit=inconsistent"
- hung: alive, but stalled
  "live: status=hung last-activity=<s>"
- tty closed: output detached but child is running
  "live: tty closed; no further output"
- gap: rotation dropped data
  "live: dropped <j> lines + <k> bytes (from-line=<N>, first-line=<F>, from-byte=<B0>, first-byte=<B1>)"
- partial: partial line (eg. progress bar)
  "live: partial-line bytes=<k> age=<s>"

Check for new data:
  live tail -vn +<N> <SELECTOR>  # by line
  live tail -vc +<B> <SELECTOR>  # by byte

Reset cursor to 1 if <uuid> changes (new session)
"""


def cmd_llms_txt(args) -> int:
    sys.stdout.write(LLMS_TXT_PAYLOAD)
    return 0


def cmd_completion_selectors(args) -> int:
    """Print selector candidates (names + ids), one per line.

    Plumbing for the shell completion scripts; scoped like `ls`.
    """
    cfg = load_config()
    sweep_all(cfg)
    sessions = list_sessions(cfg, cwd_filter=_scope_filter(args))
    if not args.all:
        sessions = [s for s in sessions if s.status in ("running", "hung")]
    tokens = {s.id for s in sessions} | {
        s.meta.name for s in sessions if s.meta.name is not None
    }
    for token in sorted(tokens):
        print(token)
    return 0


def cmd_completion_cwds(args) -> int:
    """Print the distinct cwds of all sessions, one per line.

    Plumbing for `-C/--cwd` value completion in the shell scripts.
    """
    cfg = load_config()
    sweep_all(cfg)
    for cwd in sorted({s.meta.cwd for s in list_sessions(cfg)}):
        print(cwd)
    return 0


def cmd_completion_script(args) -> int:
    from .completion import script_for

    # argparse `choices` guarantees a known shell.
    sys.stdout.write(script_for(args.shell))
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
