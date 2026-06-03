"""Recorder: PTY-wrap a child command, mirror to stdout, record to disk.

Implements the startup ordering, write-order invariant, idle heartbeat,
rotation, retention, and graceful-exit paths from DESIGN.md.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import termios
import time
import tty
import uuid
from pathlib import Path

from .config import Config
from .format import (
    IDX_RECORD,
    INCONSISTENT_MARKER,
    Meta,
    DEAD_NAME,
    LOCK_NAME,
    idx_name,
    list_segments,
    stream_name,
    write_meta_atomic,
)
from .lock import acquire_lock
from .paths import Scope, session_dir


# ioctl constants for window size — use stdlib termios where available.
TIOCGWINSZ = getattr(termios, "TIOCGWINSZ", 0x40087468)
TIOCSWINSZ = getattr(termios, "TIOCSWINSZ", 0x80087467)


class _Recorder:
    def __init__(
        self,
        scope: Scope,
        cfg: Config,
        command: list[str],
        name: str | None,
    ):
        self.scope = scope
        self.cfg = cfg
        self.command = command
        self.name = name

        self.session_id = str(uuid.uuid7())
        self.dir: Path = session_dir(scope, self.session_id)
        self.meta = Meta(
            id=self.session_id,
            command=list(command),
            cwd=os.getcwd(),
            started_at=time.time(),
            name=name,
        )

        self.lock_fd: int = -1
        self.master_fd: int = -1
        self.child_pid: int = -1

        self.active_seg: int = 0
        self.stream_fd: int = -1
        self.idx_fd: int = -1
        self.stream_bytes: int = 0  # bytes in active stream segment
        self.line_counter: int = 0  # absolute line number; n of NEXT completed line
        self.pending_line_start: float | None = None  # seconds; t for current partial
        self.pending_line_bytes: int = 0  # bytes written to stream past last `\n`

        self.last_idx_touch: float = 0.0  # seconds
        self.exited_by_signal: int | None = None
        self.inconsistent: bool = False

        self._saved_tty: list | None = None
        self._stdin_is_tty: bool = False

    # ----- startup -----

    def setup_session(self) -> None:
        """Steps 1–5 of Startup order. Step 6 (pty.fork) happens in run()."""
        # 1. mkdir 0700
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        # 2. open process.lock and flock
        self.lock_fd = acquire_lock(self.dir / LOCK_NAME, os.getpid())
        # 3. pid already written by acquire_lock
        # 4. empty stream/idx pair
        self._open_active_segment(0, create=True)
        # 5. meta.json atomic
        write_meta_atomic(self.dir, self.meta)

    def _open_active_segment(self, seg: int, *, create: bool) -> None:
        if self.stream_fd >= 0:
            try:
                os.close(self.stream_fd)
            except OSError:
                pass
        if self.idx_fd >= 0:
            try:
                os.close(self.idx_fd)
            except OSError:
                pass
        stream_path = self.dir / stream_name(seg)
        idx_path = self.dir / idx_name(seg)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        # Spec: at rotation, create stream first, then idx (reader tolerance).
        self.stream_fd = os.open(str(stream_path), flags, 0o600)
        self.idx_fd = os.open(str(idx_path), flags, 0o600)
        self.active_seg = seg
        self.stream_bytes = os.fstat(self.stream_fd).st_size
        # Reset partial-line state — rotation only happens at line boundary
        # so no partial bytes should carry across.
        self.pending_line_start = None
        self.pending_line_bytes = 0
        self.last_idx_touch = time.time()

    # ----- fork & PTY -----

    def run(self) -> int:
        """pty.fork + execvp the child, run the select loop, return child exit code."""
        if self.cfg is None:
            raise RuntimeError("config not loaded")

        # Seed initial PTY size from stdin's window before fork so the child
        # sees the right dimensions.
        winsize = self._get_winsize_from_stdin()

        self.child_pid, self.master_fd = pty.fork()
        if self.child_pid == 0:
            # Child: exec the wrapped command. On failure, exit nonzero so the
            # parent can graceful-exit with a real status.
            try:
                os.execvp(self.command[0], self.command)
            except FileNotFoundError:
                os.write(2, f"live: command not found: {self.command[0]}\n".encode())
                os._exit(127)
            except OSError as e:
                os.write(2, f"live: exec failed: {e}\n".encode())
                os._exit(126)

        # Parent
        if winsize is not None:
            self._set_winsize(self.master_fd, winsize)

        self._install_signals()
        self._raw_stdin()
        try:
            return self._select_loop()
        finally:
            self._restore_stdin()

    def _get_winsize_from_stdin(self) -> bytes | None:
        try:
            if not os.isatty(0):
                return None
            return fcntl.ioctl(0, TIOCGWINSZ, b"\x00" * 8)
        except OSError:
            return None

    def _set_winsize(self, fd: int, winsize: bytes) -> None:
        try:
            fcntl.ioctl(fd, TIOCSWINSZ, winsize)
        except OSError:
            pass

    def _raw_stdin(self) -> None:
        self._stdin_is_tty = os.isatty(0)
        if not self._stdin_is_tty:
            return
        try:
            self._saved_tty = termios.tcgetattr(0)
            tty.setraw(0)
        except (termios.error, OSError):
            self._saved_tty = None

    def _restore_stdin(self) -> None:
        if self._saved_tty is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, self._saved_tty)
            except (termios.error, OSError):
                pass

    # ----- signals -----

    def _install_signals(self) -> None:
        # wakeup_fd: a self-pipe; signal arrival makes select wake.
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        try:
            signal.set_wakeup_fd(w)
        except ValueError:
            pass
        self._wakeup_r = r
        self._wakeup_w = w

        def _on_winch(_sig, _frm):
            # Forward terminal size to child PTY.
            try:
                if os.isatty(0):
                    ws = fcntl.ioctl(0, TIOCGWINSZ, b"\x00" * 8)
                    fcntl.ioctl(self.master_fd, TIOCSWINSZ, ws)
            except OSError:
                pass

        def _on_termish(sig, _frm):
            self.exited_by_signal = sig
            # Forward to child; it will exit, our select loop will see EIO.
            try:
                os.kill(self.child_pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

        signal.signal(signal.SIGWINCH, _on_winch)
        signal.signal(signal.SIGTERM, _on_termish)
        signal.signal(signal.SIGHUP, _on_termish)
        # SIGINT: only install a handler when stdin is NOT a TTY.
        # With a TTY, line discipline routes ^C to the child's pgroup directly.
        if not os.isatty(0):
            signal.signal(signal.SIGINT, _on_termish)

    # ----- main loop -----

    def _select_loop(self) -> int:
        heartbeat = self.cfg.heartbeat_sec
        while True:
            try:
                rlist, _, _ = select.select(
                    [0, self.master_fd, self._wakeup_r], [], [], heartbeat
                )
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                raise

            now = time.time()

            # Drain wakeup pipe (signals already ran their handlers).
            if self._wakeup_r in rlist:
                try:
                    os.read(self._wakeup_r, 4096)
                except BlockingIOError:
                    pass

            # Forward stdin -> child PTY.
            if 0 in rlist:
                try:
                    data = os.read(0, 4096)
                except OSError:
                    data = b""
                if data:
                    try:
                        os.write(self.master_fd, data)
                    except OSError:
                        pass

            # Drain PTY -> mirror to stdout, record.
            if self.master_fd in rlist:
                try:
                    chunk = os.read(self.master_fd, 8192)
                except OSError:
                    chunk = b""
                if not chunk:
                    # Child closed PTY -> exited.
                    break
                # Mirror to terminal (best-effort; ignore SIGPIPE-like errors).
                try:
                    n = 0
                    while n < len(chunk):
                        n += os.write(1, chunk[n:])
                except OSError:
                    pass
                # Record.
                try:
                    self._record_chunk(chunk)
                except OSError:
                    self.inconsistent = True
                    self._kill_child()
                    break

            # Heartbeat: touch the active idx mtime if nothing's written for a while.
            if now - self.last_idx_touch >= heartbeat:
                try:
                    os.utime(self.dir / idx_name(self.active_seg), None)
                    self.last_idx_touch = now
                except OSError:
                    pass

        # Reap child.
        status = 0
        try:
            _, status = os.waitpid(self.child_pid, 0)
        except ChildProcessError:
            pass

        if self.inconsistent:
            self._stamp_dead(inconsistent=True)
            return 1

        if os.WIFEXITED(status):
            exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            exit_code = 128 + os.WTERMSIG(status)
        else:
            exit_code = 1

        # Graceful exit: update meta, then deadAt, then unlock.
        final_meta = Meta(
            id=self.meta.id,
            command=self.meta.command,
            cwd=self.meta.cwd,
            started_at=self.meta.started_at,
            exited_at=time.time(),
            exit_code=exit_code,
            name=self.meta.name,
        )
        try:
            write_meta_atomic(self.dir, final_meta)
        except OSError:
            pass
        self._stamp_dead(inconsistent=False)
        return exit_code

    # ----- record / index -----

    def _record_chunk(self, chunk: bytes) -> None:
        """Append bytes to active stream segment, index any completed lines."""
        if not chunk:
            return

        # Write stream first (prefix invariant).
        self._write_all(self.stream_fd, chunk)
        self.stream_bytes += len(chunk)
        # Track partial-line state: any byte past the last \n in chunk is partial.
        # Iterate \n positions to record idx entries and track timings.
        start = 0
        while True:
            nl = chunk.find(b"\n", start)
            if nl < 0:
                # Remainder is a partial line (no newline). Start timer if needed.
                if start < len(chunk):
                    if self.pending_line_start is None:
                        self.pending_line_start = time.time()
                    self.pending_line_bytes += len(chunk) - start
                break
            # Line completion: assign n, capture t at the line's first byte.
            t = (
                self.pending_line_start
                if self.pending_line_start is not None
                else time.time()
            )
            self.line_counter += 1
            n = self.line_counter
            try:
                self._write_all(self.idx_fd, IDX_RECORD.pack(n, t))
            except OSError:
                raise
            self.last_idx_touch = time.time()
            # Reset partial-line state for next line.
            self.pending_line_start = None
            self.pending_line_bytes = 0
            start = nl + 1

        # Rotation check: only at line boundary (no pending partial bytes).
        if self.pending_line_bytes == 0 and self.stream_bytes >= self.cfg.segment_bytes:
            self._rotate()

    def _write_all(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        n = 0
        while n < len(view):
            n += os.write(fd, view[n:])

    def _rotate(self) -> None:
        next_seg = self.active_seg + 1
        self._open_active_segment(next_seg, create=True)
        self._retain()

    def _retain(self) -> None:
        max_bytes = self.cfg.max_bytes
        segs = list_segments(self.dir).nums
        if not segs:
            return
        total = 0
        sizes: dict[int, int] = {}
        for s in segs:
            try:
                sizes[s] = os.path.getsize(self.dir / stream_name(s))
            except FileNotFoundError:
                sizes[s] = 0
            total += sizes[s]
        # Drop lowest-numbered while over the cap, but never the active segment.
        for s in segs:
            if total <= max_bytes:
                break
            if s == self.active_seg:
                break
            sp = self.dir / stream_name(s)
            ip = self.dir / idx_name(s)
            try:
                os.unlink(sp)
            except FileNotFoundError:
                pass
            try:
                os.unlink(ip)
            except FileNotFoundError:
                pass
            total -= sizes.get(s, 0)

    # ----- shutdown -----

    def _kill_child(self) -> None:
        try:
            os.kill(self.child_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(self.child_pid, 0)
        except ChildProcessError:
            pass

    def _stamp_dead(self, *, inconsistent: bool) -> None:
        """Create deadAt (O_EXCL) BEFORE releasing the lock fd."""
        dead = self.dir / DEAD_NAME
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(str(dead), flags, 0o600)
            if inconsistent:
                os.write(fd, INCONSISTENT_MARKER)
            os.close(fd)
        except FileExistsError:
            # A sweeper raced us; harmless.
            pass
        finally:
            if self.lock_fd >= 0:
                os.close(self.lock_fd)
                self.lock_fd = -1


def record(
    scope: Scope,
    cfg: Config,
    command: list[str],
    name: str | None = None,
) -> int:
    """Run `command` under live recording. Returns its exit code."""
    rec = _Recorder(scope, cfg, command, name)
    rec.setup_session()
    return rec.run()
