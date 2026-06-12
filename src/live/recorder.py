"""Recorder: PTY-wrap a child command, mirror to stdout, record to disk.

Holds the sole writer flock on `process.lock` for the session's lifetime.
Stream is always one complete line ahead of the index (prefix invariant);
heartbeat touches the active idx mtime to surface `hung` vs silent.

Rotation happens at exactly `segment_bytes`, mid-line if that's where the
budget lands — a line may span segments, and pending-line state survives
rotation. `maxKb` is a hard cap: retention runs on every rotation and is
never blocked by an unterminated line.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time
import tty
import uuid
from pathlib import Path

from .config import Config
from .format import (
    IDX_HEADER,
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
from .paths import session_dir


# ioctl constants for window size — use stdlib termios where available.
TIOCGWINSZ = getattr(termios, "TIOCGWINSZ", 0x40087468)
TIOCSWINSZ = getattr(termios, "TIOCSWINSZ", 0x80087467)

# PTY size when there's no controlling terminal and no --geometry.
DEFAULT_GEOMETRY = (80, 24)  # (cols, rows)

# After forwarding SIGTERM/SIGHUP, SIGKILL the child's process group if it
# hasn't exited. Must undercut STOP_KILL_DEADLINE_SEC so the recorder still
# exits gracefully (meta -> deadAt -> unlock) before `live stop` SIGKILLs it.
TERM_KILL_GRACE_SEC = 3.0

# How long `live stop` waits for the recorder's flock release after SIGTERM
# before SIGKILLing the recorder itself.
STOP_KILL_DEADLINE_SEC = 5.0


class _Recorder:
    def __init__(
        self,
        cfg: Config,
        command: list[str],
        name: str | None,
        detach: bool = False,
        geometry: tuple[int, int] | None = None,
        *,
        cwd: Path,
    ):
        self.cfg = cfg
        self.command = command
        self.name = name
        self.detach = detach
        self.geometry = geometry
        self.cwd = cwd

        self.session_id = str(uuid.uuid4())
        self.dir: Path = session_dir(self.session_id)
        self.meta = Meta(
            id=self.session_id,
            command=list(command),
            cwd=str(cwd),
            started_at=time.time(),
            name=name,
        )

        self.lock_fd: int = -1
        self.master_fd: int = -1
        self.child_pid: int = -1

        # Cap wins: a segment budget above maxKb would void the retention bound.
        self.segment_bytes: int = min(cfg.segment_bytes, cfg.max_bytes)

        self.active_seg: int = 0
        self.stream_fd: int = -1
        self.idx_fd: int = -1
        self.stream_bytes: int = 0  # bytes in active stream segment
        self.lifetime_bytes: int = 0  # cumulative bytes ever written (all segments)
        self.line_counter: int = 0  # absolute line number; n of NEXT completed line
        self.pending_line_start: float | None = None  # seconds; t for current partial
        self.pending_line_start_byte: int | None = None  # lifetime byte of partial's first byte
        self.pending_line_bytes: int = 0  # bytes written to stream past last `\n`

        self.last_idx_touch: float = 0.0  # seconds
        self.exited_by_signal: int | None = None
        self.term_at: float | None = None  # first SIGTERM/SIGHUP arrival
        self.inconsistent: bool = False

        self._saved_tty: list | None = None
        self._stdin_is_tty: bool = False

    # ----- startup -----

    def setup_session(self) -> None:
        """Create dir + lock + stream/idx + meta before any reader can see them."""
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.lock_fd = acquire_lock(self.dir / LOCK_NAME, os.getpid())
        self._open_active_segment(0, create=True)
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
        # Stream before idx — readers tolerate stream-without-idx, not the inverse.
        self.stream_fd = os.open(str(stream_path), flags, 0o600)
        self.idx_fd = os.open(str(idx_path), flags, 0o600)
        self.active_seg = seg
        self.stream_bytes = os.fstat(self.stream_fd).st_size
        # Write the header on first open of a fresh idx: segment start, plus
        # where the line open at that point began (lets readers report how
        # many bytes of a head-truncated line retention dropped).
        if os.fstat(self.idx_fd).st_size == 0:
            line_start = (
                self.pending_line_start_byte
                if self.pending_line_bytes and self.pending_line_start_byte is not None
                else self.lifetime_bytes
            )
            self._write_all(
                self.idx_fd, IDX_HEADER.pack(self.lifetime_bytes, line_start)
            )
        # pending_line_* is NOT reset: a partial line spans rotation.
        self.last_idx_touch = time.time()

    # ----- fork & PTY -----

    def run(self) -> int:
        """pty.fork + execvp the child, run the select loop, return child exit code."""
        # Seed the PTY size: explicit --geometry wins, else stdin's window,
        # else 80x24 — a TTY-less recorder must not leave the child at 0x0.
        if self.geometry is not None:
            cols, rows = self.geometry
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
        else:
            winsize = self._get_winsize_from_stdin()
            if winsize is None:
                cols, rows = DEFAULT_GEOMETRY
                winsize = struct.pack("HHHH", rows, cols, 0, 0)

        self.child_pid, self.master_fd = pty.fork()
        if self.child_pid == 0:
            # Child: exec the wrapped command. On failure, exit nonzero so the
            # parent can graceful-exit with a real status.
            try:
                os.chdir(self.cwd)
            except OSError as e:
                os.write(2, f"live: chdir failed: {e}\n".encode())
                os._exit(126)
            try:
                os.execvp(self.command[0], self.command)
            except FileNotFoundError:
                os.write(2, f"live: command not found: {self.command[0]}\n".encode())
                os._exit(127)
            except OSError as e:
                os.write(2, f"live: exec failed: {e}\n".encode())
                os._exit(126)

        # Parent
        self._set_winsize(self.master_fd, winsize)

        self._set_terminal_title()
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

    def _set_terminal_title(self) -> None:
        # OSC 0 ; <text> BEL — without this, terminals that derive the tab
        # title from the foreground process (notably VSCode) show "Python".
        if not os.isatty(1):
            return
        title = os.path.basename(self.command[0]) or self.command[0]
        try:
            os.write(1, f"\x1b]0;{title}\x07".encode("utf-8", "replace"))
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
            if self.term_at is None:
                self.term_at = time.time()
            # Forward to child; it will exit, our select loop will see EIO.
            # If it ignores the signal, the loop escalates to SIGKILL after
            # TERM_KILL_GRACE_SEC.
            try:
                os.kill(self.child_pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

        # Explicit --geometry pins the size; don't track terminal resizes.
        if self.geometry is None:
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
        watch = [self.master_fd, self._wakeup_r]
        if not self.detach:
            watch.append(0)
        while True:
            timeout = heartbeat
            if self.term_at is not None:
                deadline = self.term_at + TERM_KILL_GRACE_SEC
                timeout = min(timeout, max(deadline - time.time(), 0))
            try:
                rlist, _, _ = select.select(watch, [], [], timeout)
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                raise

            now = time.time()

            # A forwarded SIGTERM/SIGHUP the child ignored must not leave it
            # running past `live stop`'s deadline: SIGKILL its process group.
            if self.term_at is not None and now - self.term_at >= TERM_KILL_GRACE_SEC:
                self.term_at = None
                self._kill_child_group()

            # Drain wakeup pipe (signals already ran their handlers).
            if self._wakeup_r in rlist:
                try:
                    os.read(self._wakeup_r, 4096)
                except BlockingIOError:
                    pass

            # Forward stdin -> child PTY. On EOF, stop watching fd 0 or
            # select would report it readable forever.
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
                else:
                    watch.remove(0)

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
                if not self.detach:
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
        """Append bytes to the active stream segment, index any completed
        lines. Splits at the segment budget and rotates unconditionally —
        mid-line if that's where the budget lands — so closed segments are
        exactly `segment_bytes` and retention is never blocked by an
        unterminated line."""
        while chunk:
            budget = self.segment_bytes - self.stream_bytes
            piece, chunk = chunk[:budget], chunk[budget:]
            self._record_piece(piece)
            if self.stream_bytes >= self.segment_bytes:
                self._rotate()

    def _record_piece(self, piece: bytes) -> None:
        # Write stream first (prefix invariant).
        piece_start_lifetime = self.lifetime_bytes
        self._write_all(self.stream_fd, piece)
        self.stream_bytes += len(piece)
        self.lifetime_bytes += len(piece)
        start = 0
        while True:
            nl = piece.find(b"\n", start)
            if nl < 0:
                if start < len(piece):
                    if self.pending_line_start is None:
                        self.pending_line_start = time.time()
                        self.pending_line_start_byte = piece_start_lifetime + start
                    self.pending_line_bytes += len(piece) - start
                break
            # `t` / `byte_offset` for this line = timestamp / lifetime byte of its first byte.
            t = (
                self.pending_line_start
                if self.pending_line_start is not None
                else time.time()
            )
            line_start_byte = (
                self.pending_line_start_byte
                if self.pending_line_start_byte is not None
                else piece_start_lifetime + start
            )
            self.line_counter += 1
            n = self.line_counter
            try:
                self._write_all(self.idx_fd, IDX_RECORD.pack(n, t, line_start_byte))
            except OSError:
                raise
            self.last_idx_touch = time.time()
            self.pending_line_start = None
            self.pending_line_start_byte = None
            self.pending_line_bytes = 0
            start = nl + 1

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
        """Drop lowest-numbered segments while over the cap. The active
        segment and the newest closed one are always kept: the newest closed
        idx holds the latest line records (watermarks must never regress),
        and since closed segments are at most `segment_bytes <= max_bytes`,
        retention is hard-bounded at `max_bytes + segment_bytes`."""
        max_bytes = self.cfg.max_bytes
        segs = list_segments(self.dir)
        total = 0
        sizes: dict[int, int] = {}
        for s in segs:
            try:
                sizes[s] = os.path.getsize(self.dir / stream_name(s))
            except FileNotFoundError:
                sizes[s] = 0
            total += sizes[s]
        # Only called from _rotate, so segs[-1] is the active segment and
        # segs[-2] the newest closed one.
        for s in segs[:-2]:
            if total <= max_bytes:
                break
            try:
                os.unlink(self.dir / stream_name(s))
            except FileNotFoundError:
                pass
            try:
                os.unlink(self.dir / idx_name(s))
            except FileNotFoundError:
                pass
            total -= sizes.get(s, 0)

    # ----- shutdown -----

    def _kill_child_group(self) -> None:
        """SIGKILL the child's process group (pty.fork made it a session
        leader, so pgid == pid and same-group descendants die with it).
        No reap here; the select loop exits normally on PTY EOF."""
        try:
            os.killpg(self.child_pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(self.child_pid, signal.SIGKILL)
            except OSError:
                pass

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
    cfg: Config,
    command: list[str],
    name: str | None = None,
    geometry: tuple[int, int] | None = None,
    *,
    cwd: Path,
    after_setup=None,
) -> int:
    """Run `command` under live recording. Returns its exit code.

    `cwd` is the child's working directory and the session's scope.
    `after_setup` (if given) runs once the session is publicly visible
    (dir + lock + meta), before the child is forked.
    """
    rec = _Recorder(cfg, command, name, geometry=geometry, cwd=cwd)
    rec.setup_session()
    if after_setup is not None:
        after_setup()
    return rec.run()


def _fd_above_std(fd: int) -> int:
    """Move `fd` to >= 3 — the detach dup2 dance clobbers 0-2, which the
    pipe may occupy when the CLI was started with closed std fds."""
    if fd > 2:
        return fd
    new_fd = fcntl.fcntl(fd, fcntl.F_DUPFD, 3)
    os.close(fd)
    return new_fd


def record_detached(
    cfg: Config,
    command: list[str],
    name: str | None = None,
    geometry: tuple[int, int] | None = None,
    *,
    cwd: Path,
) -> tuple[str | None, str | None]:
    """Fork a recorder that survives the calling shell; don't wait for it.

    The child detaches (`setsid`, fds on /dev/null) and reports back over a
    pipe once the session dir + lock exist, so the session is visible to
    `live ls` by the time this returns. Returns `(session_id, error)`:
    exactly one is non-None.
    """
    read_fd, write_fd = os.pipe()
    read_fd = _fd_above_std(read_fd)
    write_fd = _fd_above_std(write_fd)
    pid = os.fork()
    if pid == 0:
        # Child: never return into the caller's stack.
        status = 1
        try:
            os.close(read_fd)
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            if devnull > 2:
                os.close(devnull)
            os.setsid()
            rec = _Recorder(
                cfg, command, name, detach=True, geometry=geometry, cwd=cwd
            )
            try:
                rec.setup_session()
            except Exception as e:
                os.write(write_fd, f"err {e}\n".encode("utf-8", "replace"))
                os._exit(1)
            os.write(write_fd, f"ok {rec.session_id}\n".encode("ascii"))
            os.close(write_fd)
            status = rec.run()
        except BaseException:
            pass
        finally:
            os._exit(status)

    # Parent: read the child's one-line report, then leave it running.
    os.close(write_fd)
    chunks = []
    while True:
        try:
            buf = os.read(read_fd, 4096)
        except OSError:
            break
        if not buf:
            break
        chunks.append(buf)
        if buf.endswith(b"\n"):
            break
    os.close(read_fd)
    report = b"".join(chunks).decode("utf-8", "replace").strip()
    if report.startswith("ok "):
        return report[3:], None
    if report.startswith("err "):
        return None, report[4:]
    return None, "detached recorder failed to start"
