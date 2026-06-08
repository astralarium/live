"""Interactive curses pager for `live less`.

Layered for testability:
  - `PagerState`: pure in-memory view model (scroll, seen benchmark, follow flag).
  - `load_lines`: snapshot the session into `Line` records.
  - `run_pager`: curses I/O loop driving state + rendering.

When stdout is not a TTY, `run_pager` degrades to cat semantics.
"""

from __future__ import annotations

import curses
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .format import LOCK_NAME, idx_name, list_segments, read_idx_records
from .lock import probe_held
from .reader import (
    cat_all,
    lines_in_segment,
    segment_refs,
    stream_segment_bytes,
    strip_ansi,
)
from .session import SessionInfo, session_info
from .watcher import new_watcher

# ----- data model -----


@dataclass(frozen=True)
class Line:
    """One recorded line. `text` includes its trailing newline.

    `end_byte` is the cumulative on-disk byte offset through the end of this
    line, used by the pager status bar to show byte position within the
    rendered view.
    """

    text: bytes
    n: int
    t: float
    end_byte: int


@dataclass
class _ScanCursor:
    """Walk position shared between the initial snapshot and incremental drains.

    Tracks the segment number last scanned, how many completed lines were
    consumed from it, and the cumulative byte count through those lines. The
    drain thread keeps one of these; advancing it lets the next call skip every
    segment before `seg` and resume mid-way through `seg` itself.
    """

    seg: int | None = None
    records_in_seg: int = 0
    total_bytes: int = 0


def _scan_from(session_dir: Path, cursor: _ScanCursor) -> list[Line]:
    """Walk segments forward from `cursor`, return new Lines, advance `cursor`.

    Segments numbered below `cursor.seg` are skipped — they were drained on a
    previous call. If `cursor.seg` is no longer present (unexpected rotation
    or pruning), the cursor resets and the scan restarts from segment 0.
    """
    refs = segment_refs(session_dir)
    if not refs:
        return []
    if cursor.seg is None:
        start_idx = 0
    else:
        start_idx = next(
            (i for i, r in enumerate(refs) if r.seg == cursor.seg), None
        )
        if start_idx is None:
            cursor.seg = None
            cursor.records_in_seg = 0
            cursor.total_bytes = 0
            start_idx = 0

    out: list[Line] = []
    cumulative = cursor.total_bytes
    for i in range(start_idx, len(refs)):
        ref = refs[i]
        records = read_idx_records(ref.idx_path)
        stream = stream_segment_bytes(ref.stream_path)
        chunks = lines_in_segment(stream, records)
        skip = cursor.records_in_seg if ref.seg == cursor.seg else 0
        consumed = skip
        for j in range(skip, len(records)):
            if j >= len(chunks):
                break
            n, t, _b = records[j]
            text = chunks[j]
            cumulative += len(text)
            out.append(Line(text=text, n=n, t=t, end_byte=cumulative))
            consumed = j + 1
        cursor.seg = ref.seg
        cursor.records_in_seg = consumed
    cursor.total_bytes = cumulative
    return out


def load_lines(session_dir: Path) -> list[Line]:
    """Snapshot every complete line from a session into `Line` records.

    Partial-line bytes at the tail of the active segment are excluded — the
    pager only displays indexed lines (lines with idx records).
    """
    return _scan_from(session_dir, _ScanCursor())


# ----- pure view state -----


@dataclass
class PagerState:
    """Pager view model. No I/O. All transitions are method calls.

    `seen` is the highest line number the user has had visible. `new_count` is
    `lines[-1].n - seen` — the "+K new" counter on the status line.
    """

    lines: list[Line] = field(default_factory=list)
    view_top: int = 0
    view_height: int = 24
    seen: int = 0
    follow: bool = False
    state_badge: str = "running"
    flash_msg: str = ""
    flash_until: float = 0.0  # epoch seconds; flash hides when time.time() passes
    # Search / prompt (less-style).
    search_pattern: str = ""
    search_direction: str = "forward"  # "forward" | "backward"
    search_last_match: int | None = None  # index into `lines` of last hit
    prompt_active: bool = False
    prompt_direction: str = "forward"
    prompt_buffer: str = ""
    help_active: bool = False
    help_view_top: int = 0  # scroll position within the help overlay

    def __post_init__(self) -> None:
        # On load, everything already in the buffer counts as "seen" — the
        # +N counter only ticks for lines that arrive *after* the pager opens.
        if self.lines:
            self.seen = self.lines[-1].n

    # ----- mutations -----

    def resize(self, height: int) -> None:
        new_h = max(1, height)
        # Only snap view_top on an actual height change. The render loop calls
        # resize() every frame to stay in sync with the terminal, and
        # unconditional snap-back would undo search/scroll-past-end positions.
        height_changed = new_h != self.view_height
        self.view_height = new_h
        if height_changed:
            self.view_top = min(self.view_top, self._natural_max_view_top())
        self._update_seen()

    def feed_lines(self, new_lines: list[Line]) -> None:
        if not new_lines:
            return
        self.lines.extend(new_lines)
        if self.follow:
            self.goto_end()
        else:
            self._update_seen()

    def scroll_down(self, n: int = 1) -> None:
        # Movement keys must not push view_top into the `~` zone (past the
        # natural max where the buffer no longer fills the viewport). Only
        # search is allowed to overscroll there. Once past, movement out is
        # only via scroll_up / goto_end / goto_start.
        natural = self._natural_max_view_top()
        if self.view_top >= natural:
            return  # at natural max (or beyond, via search) — j is a no-op
        self.view_top = min(natural, self.view_top + n)
        self._update_seen()

    def scroll_up(self, n: int = 1) -> None:
        self.view_top = max(0, self.view_top - n)

    def page_down(self) -> None:
        self.scroll_down(self.view_height)

    def page_up(self) -> None:
        self.scroll_up(self.view_height)

    def half_page_down(self) -> None:
        self.scroll_down(max(1, self.view_height // 2))

    def half_page_up(self) -> None:
        self.scroll_up(max(1, self.view_height // 2))

    def goto_start(self) -> None:
        self.view_top = 0

    def goto_end(self) -> None:
        # Last line at the bottom of the viewport (full screen of content),
        # not last line at the top — that would leave the screen mostly `~`.
        self.view_top = self._natural_max_view_top()
        self._update_seen()

    def goto_line(self, n: int) -> None:
        """Position the view so line number `n` is the top row (less `Ng`/`NG`).

        `n` is the recorder line number (Line.n), 1-indexed. Out-of-range
        values clamp to the first/last line in the buffer.
        """
        if not self.lines:
            return
        target = 0
        for i, line in enumerate(self.lines):
            if line.n >= n:
                target = i
                break
        else:
            target = len(self.lines) - 1
        self.view_top = max(0, min(self._max_view_top(), target))
        self._update_seen()

    def toggle_follow(self) -> None:
        if not self.follow and self.state_badge != "running":
            self.set_flash("(session not running)")
            return
        self.follow = not self.follow
        if self.follow:
            self.goto_end()

    def set_state_badge(self, badge: str) -> None:
        """Update the session-state badge. Leaving 'running' also drops follow,
        since there's nothing new to follow."""
        self.state_badge = badge
        if badge != "running":
            self.follow = False

    def toggle_help(self) -> None:
        self.help_active = not self.help_active
        if not self.help_active:
            self.help_view_top = 0

    def _help_max_top(self) -> int:
        return max(0, len(_HELP_LINES) - self.view_height)

    def help_scroll_down(self, n: int = 1) -> None:
        self.help_view_top = min(self._help_max_top(), self.help_view_top + n)

    def help_scroll_up(self, n: int = 1) -> None:
        self.help_view_top = max(0, self.help_view_top - n)

    def help_page_down(self) -> None:
        self.help_scroll_down(self.view_height)

    def help_page_up(self) -> None:
        self.help_scroll_up(self.view_height)

    def help_goto_start(self) -> None:
        self.help_view_top = 0

    def help_goto_end(self) -> None:
        self.help_view_top = self._help_max_top()

    def set_flash(self, msg: str, *, duration: float = 2.0) -> None:
        """Show `msg` in the status badge slot for `duration` seconds, then decay."""
        self.flash_msg = msg
        self.flash_until = time.time() + duration

    # ----- search / prompt -----

    def start_prompt(self, direction: str) -> None:
        """Enter pattern-entry mode for `/` (forward) or `?` (backward)."""
        self.prompt_active = True
        self.prompt_direction = direction
        self.prompt_buffer = ""
        self.follow = False  # entering a search disables auto-follow

    def append_prompt(self, ch: str) -> None:
        if self.prompt_active:
            self.prompt_buffer += ch

    def backspace_prompt(self) -> None:
        if self.prompt_active and self.prompt_buffer:
            self.prompt_buffer = self.prompt_buffer[:-1]

    def cancel_prompt(self) -> None:
        self.prompt_active = False
        self.prompt_buffer = ""

    def submit_prompt(self) -> bool:
        """Execute the entered pattern. Returns True if a match was found."""
        pattern = self.prompt_buffer
        direction = self.prompt_direction
        self.prompt_active = False
        self.prompt_buffer = ""
        if not pattern:
            return False
        return self.search(pattern, direction)

    def search(self, pattern: str, direction: str) -> bool:
        """Find the next match of `pattern` and scroll it to the top of view.

        Smart-case: case-insensitive when `pattern` is all lowercase (mirrors
        `less -i`). Sets a flash on regex error or no-match.
        """
        regex = self._compile(pattern)
        if regex is None:
            return False
        self.search_pattern = pattern
        self.search_direction = direction
        return self._jump_to_match(regex, direction)

    def search_repeat(self, *, reverse: bool = False) -> bool:
        """Re-run the last search. `reverse=True` flips direction (`N` key).

        Search starts from the current `view_top`, mirroring less: any movement
        between searches resets the "search from" position.
        """
        if not self.search_pattern:
            self.set_flash("(no previous search)")
            return False
        regex = self._compile(self.search_pattern)
        if regex is None:
            return False
        direction = self.search_direction
        if reverse:
            direction = "backward" if direction == "forward" else "forward"
        return self._jump_to_match(regex, direction)

    def _compile(self, pattern: str, *, silent: bool = False) -> "re.Pattern[str] | None":
        """Smart-case compile (case-insensitive when pattern is all lowercase).
        On regex error: flashes a hint unless `silent=True` (render path)."""
        flags = re.IGNORECASE if pattern == pattern.lower() else 0
        try:
            return re.compile(pattern, flags)
        except re.error as e:
            if not silent:
                self.set_flash(f"(bad regex: {e})")
            return None

    def _jump_to_match(
        self,
        regex: "re.Pattern[str]",
        direction: str,
    ) -> bool:
        """Find the next match in `direction` from `view_top` and scroll it to
        the top of the viewport.

        Search starts at `view_top` inclusive. If the user hasn't moved since
        the last match (`view_top == search_last_match`), skip one line so `n`
        advances past the current hit. Any movement resets `view_top`, so
        searching from the new position is automatic.
        """
        if not self.lines:
            self.set_flash("(pattern not found)")
            return False

        skip = self.search_last_match == self.view_top
        if direction == "forward":
            start = self.view_top + (1 if skip else 0)
            rng = range(start, len(self.lines))
        else:
            start = self.view_top - (1 if skip else 0)
            rng = range(start, -1, -1)

        for i in rng:
            if regex.search(self._decode(self.lines[i])):
                self.search_last_match = i
                # Scroll match to top of viewport (clamped at the tail).
                self.view_top = max(0, min(self._max_view_top(), i))
                self._update_seen()
                return True
        self.set_flash("(pattern not found)")
        return False

    def visible_matches(self) -> list[tuple[int, int, int]]:
        """Per-viewport match spans: `(row, col_start, col_end)`.

        `row` is the row offset within the viewport. Used by the renderer to
        highlight matches.
        """
        if not self.search_pattern:
            return []
        regex = self._compile(self.search_pattern, silent=True)
        if regex is None:
            return []
        out: list[tuple[int, int, int]] = []
        for row, line in enumerate(self.visible()):
            text = self._decode(line).rstrip("\r\n")
            for m in regex.finditer(text):
                if m.end() == m.start():
                    continue  # zero-width match; nothing to paint
                out.append((row, m.start(), m.end()))
        return out

    @staticmethod
    def _decode(line: Line) -> str:
        return line.text.decode("utf-8", errors="replace")

    # ----- derived state -----

    def visible(self) -> list[Line]:
        return self.lines[self.view_top : self.view_top + self.view_height]

    def view_bottom_line(self) -> Line | None:
        vis = self.visible()
        return vis[-1] if vis else None

    def new_count(self) -> int:
        if not self.lines:
            return 0
        return max(0, self.lines[-1].n - self.seen)

    def status_text(self, session_id: str) -> str:
        bot = self.view_bottom_line()
        last = self.lines[-1] if self.lines else None
        if bot is None or last is None:
            at_line = "0/0"
            at_byte = "0/0"
            at_time = "0.000/0.000"
        else:
            at_line = f"{bot.n}/{last.n}"
            at_byte = f"{bot.end_byte}/{last.end_byte}"
            at_time = f"{bot.t:.3f}/{last.t:.3f}"
        parts = [
            f"id={session_id[:8]}"
            f" at-line={at_line}"
            f" at-byte={at_byte}"
            f" at-time={at_time}"
        ]
        new = self.new_count()
        if new > 0:
            parts.append(f"[+{new} new]")
        if self.follow:
            parts.append("[FOLLOW] (^X or interrupt to abort)")
        right = self._active_flash() or self.state_badge
        body = "  ".join(parts)
        return body + ("  " + right if right else "")

    def _active_flash(self) -> str:
        if self.flash_msg and time.time() < self.flash_until:
            return self.flash_msg
        return ""

    # ----- helpers -----

    def _max_view_top(self) -> int:
        # Last line at the TOP of the screen (so any line — including the very
        # last — can be scrolled into the top row by search or movement). Rows
        # below the buffer are filled with `~` placeholders at render time.
        return max(0, len(self.lines) - 1)

    def _natural_max_view_top(self) -> int:
        # Last line at the BOTTOM of the screen — the furthest down movement
        # keys are allowed to scroll without leaving `~` rows.
        return max(0, len(self.lines) - self.view_height)

    def _update_seen(self) -> None:
        bot = self.view_bottom_line()
        if bot is not None:
            self.seen = max(self.seen, bot.n)


# ----- live source -----


@dataclass(frozen=True)
class SourceEvent:
    """Lifecycle signal from the background source thread.

    `kind`: "exit" | "hung" | "removed".
    """

    kind: str
    info: SessionInfo | None = None
    last_activity: float = 0.0


class PagerSource:
    """Background thread that watches a session and pushes new `Line`s + lifecycle
    `SourceEvent`s onto a thread-safe queue.

    Main loop drains via `get_nowait()` between key reads.
    """

    def __init__(
        self,
        session_dir: Path,
        cfg: Config,
        *,
        initial_cursor: int,
        scan_resume: _ScanCursor | None = None,
    ) -> None:
        self.session_dir = session_dir
        self.cfg = cfg
        self.cursor = initial_cursor
        self.queue: queue.Queue[Line | SourceEvent] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hung_emitted = False
        self._exit_emitted = False
        self._removed_emitted = False
        # When the caller already snapshot the session with `_scan_from`, pass
        # its cursor here so drains start where the snapshot left off.
        self._scan = scan_resume if scan_resume is not None else _ScanCursor()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        watcher = new_watcher()
        active_idx_path: Path | None = None
        try:
            try:
                watcher.add_path(self.session_dir)
            except OSError:
                pass
            segs = list_segments(self.session_dir)
            if segs:
                active_idx_path = self.session_dir / idx_name(segs[-1])
                try:
                    watcher.add_path(active_idx_path)
                except OSError:
                    active_idx_path = None

            while not self._stop.is_set():
                try:
                    watcher.wait(0.5)
                except OSError:
                    pass
                if self._stop.is_set():
                    break

                # Detect `live rm`. `rmtree` deletes children before the dir
                # itself, so we may see "session_dir exists but empty" before
                # `session_dir.exists()` flips false. Both are removal.
                segs = list_segments(self.session_dir)
                if not self.session_dir.exists() or not segs:
                    if not self._removed_emitted:
                        self.queue.put(SourceEvent(kind="removed"))
                        self._removed_emitted = True
                    break

                new_active = self.session_dir / idx_name(segs[-1])
                if active_idx_path != new_active:
                    if active_idx_path is not None:
                        try:
                            watcher.remove_path(active_idx_path)
                        except OSError:
                            pass
                    active_idx_path = new_active
                    try:
                        watcher.add_path(active_idx_path)
                    except OSError:
                        pass

                self._drain_new_lines()

                held = probe_held(self.session_dir / LOCK_NAME)
                if held is True:
                    try:
                        mtime = (self.session_dir / idx_name(segs[-1])).stat().st_mtime
                    except FileNotFoundError:
                        mtime = time.time()
                    if (
                        time.time() - mtime > 3 * self.cfg.heartbeat_sec
                        and not self._hung_emitted
                    ):
                        self.queue.put(SourceEvent(kind="hung", last_activity=mtime))
                        self._hung_emitted = True
                    continue

                # Lock file gone (None) or dir gone mid-iteration -> removal,
                # not a clean exit. Without this, `live rm` races the exit path.
                if held is None or not self.session_dir.exists():
                    if not self._removed_emitted:
                        self.queue.put(SourceEvent(kind="removed"))
                        self._removed_emitted = True
                    break

                # Lock released — recorder exited. Final drain, emit once, halt.
                self._drain_new_lines()
                if not self._exit_emitted:
                    info = session_info(self.session_dir, self.cfg)
                    self.queue.put(SourceEvent(kind="exit", info=info))
                    self._exit_emitted = True
                break
        finally:
            watcher.close()

    def _drain_new_lines(self) -> None:
        """Push newly indexed lines onto the queue, resuming where we left off.

        Cost is O(active-segment bytes + new lines) per wakeup — old segments
        before `self._scan.seg` are not re-read.
        """
        emitted = False
        for line in _scan_from(self.session_dir, self._scan):
            if line.n > self.cursor:
                self.queue.put(line)
                self.cursor = line.n
                emitted = True
        if emitted:
            self._hung_emitted = False  # fresh activity clears prior hung warning


def _exit_badge(info: SessionInfo | None) -> str:
    if info is None:
        return "exited"
    if info.status == "inconsistent":
        return "inconsistent"
    if info.exit_code is not None:
        return f"exited(code={info.exit_code})"
    return "exited"


# Less-style key sets. Shared between main-mode and help-overlay dispatch.
# `\n` and `\r` cover Enter on different terminals; the digits/^N/^P/etc are
# the less defaults from `less --help`.
_KEYS_LINE_DOWN = frozenset({ord("e"), 5, ord("j"), 14, ord("\n"), ord("\r"), curses.KEY_DOWN})
_KEYS_LINE_UP = frozenset({ord("y"), 25, ord("k"), 11, 16, curses.KEY_UP})
_KEYS_WINDOW_DOWN = frozenset({ord("f"), 6, 22, ord(" "), curses.KEY_NPAGE})
_KEYS_WINDOW_UP = frozenset({ord("b"), 2, curses.KEY_PPAGE})
_KEYS_HALF_DOWN = frozenset({ord("d"), 4})
_KEYS_HALF_UP = frozenset({ord("u"), 21})
_KEYS_GOTO_START = frozenset({ord("g"), curses.KEY_HOME})
_KEYS_GOTO_END = frozenset({ord("G"), curses.KEY_END})


_HELP_LINES = [
    "live less — interactive pager",
    "",
    "  MOVING (movement may be preceded by a count N)",
    "",
    "    e ^E j ^N ↓ CR     Forward one line  (or N lines)",
    "    y ^Y k ^K ^P ↑     Backward one line (or N lines)",
    "    f ^F ^V SPACE PgDn Forward one window (or N lines)",
    "    b ^B PgUp          Backward one window (or N lines)",
    "    d ^D               Forward half-window (or N lines)",
    "    u ^U               Backward half-window (or N lines)",
    "    g  Home            Goto first line     (Ng = goto line N)",
    "    G  End             Goto last line      (NG = goto line N)",
    "",
    "  SEARCH",
    "",
    "    /pattern           Forward search; first match scrolls to top",
    "    ?pattern           Backward search",
    "    n                  Repeat search in same direction",
    "    N                  Repeat search reversed",
    "    Esc                Cancel pending search prompt or count",
    "",
    "  LIVE",
    "",
    "    F                  Follow new output (^C or ^X cancels follow)",
    "",
    "  DISPLAY",
    "",
    "    r ^L ^R            Repaint screen",
    "    h H                Toggle this help",
    "    q                  Quit",
    "",
    "  Press q, h, or Esc to close.",
]


def _dispatch_scroll(
    ch: int,
    n: int | None,
    *,
    down,
    up,
    page_down,
    page_up,
    half_down,
    half_up,
) -> bool:
    """Common less-style scroll dispatch. Returns True if `ch` was handled.

    `n` is the pending numeric prefix (or None). For line keys, `n or 1` is the
    count. For window/half keys, `n` (when given) means N *lines*, not N windows
    — matching less's `Nf`/`Nb`/`Nd`/`Nu` semantics.
    """
    if ch in _KEYS_LINE_DOWN:
        down(n or 1)
    elif ch in _KEYS_LINE_UP:
        up(n or 1)
    elif ch in _KEYS_WINDOW_DOWN:
        down(n) if n else page_down()
    elif ch in _KEYS_WINDOW_UP:
        up(n) if n else page_up()
    elif ch in _KEYS_HALF_DOWN:
        down(n) if n else half_down()
    elif ch in _KEYS_HALF_UP:
        up(n) if n else half_up()
    else:
        return False
    return True


def _render_help(stdscr, w: int, content_rows: int, top: int) -> None:
    for i in range(content_rows):
        idx = top + i
        if 0 <= idx < len(_HELP_LINES):
            s = _HELP_LINES[idx]
        else:
            s = "~"
        s = s[: max(0, w - 1)]
        try:
            stdscr.addstr(i, 0, s)
        except curses.error:
            pass


# ----- curses I/O -----


def run_pager(info: SessionInfo, cfg: Config, *, strip: bool) -> int:
    """Open the pager on `info`. Falls back to cat when stdout isn't a TTY."""
    if not sys.stdout.isatty():
        return _cat_fallback(info, strip=strip)
    scan = _ScanCursor()
    lines = _scan_from(info.path, scan)
    state = PagerState(lines=lines, state_badge=info.status)
    initial_cursor = lines[-1].n if lines else 0
    source = PagerSource(
        info.path, cfg, initial_cursor=initial_cursor, scan_resume=scan
    )
    source.start()
    try:
        return curses.wrapper(_curses_loop, state, info, source, strip)
    except KeyboardInterrupt:
        return 130
    finally:
        source.stop()


def _cat_fallback(info: SessionInfo, *, strip: bool) -> int:
    result = cat_all(info.path)
    out = strip_ansi(result.stdout) if strip else result.stdout
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass
    return 0


def _curses_loop(
    stdscr,
    state: PagerState,
    info: SessionInfo,
    source: PagerSource,
    strip: bool,
) -> int:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(200)  # ms; wakes the loop to drain source queue
    # raw() so ^C / ^X arrive as bytes (3 / 24) and we can route them
    # contextually (quit when paging, cancel-follow when following).
    try:
        curses.raw()
    except curses.error:
        pass
    # Shorten the ambiguous-Esc delay so cancelling a search prompt with Esc
    # doesn't feel laggy. Default is 1000ms in many ncurses builds.
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    # Inherit the terminal's default fg/bg so unpainted cells (short lines,
    # blank rows past content) don't show as opaque black on themed terminals.
    try:
        curses.start_color()
        curses.use_default_colors()
        stdscr.bkgd(" ", curses.color_pair(0))
    except curses.error:
        pass

    count_buffer = ""

    while True:
        h, _w = stdscr.getmaxyx()
        state.resize(max(1, h - 1))
        _drain_source(source, state)
        _render(stdscr, state, info, strip=strip, count_buffer=count_buffer)

        ch = stdscr.getch()
        if ch == -1:  # timeout
            continue
        if ch == curses.KEY_RESIZE:
            continue

        # Search-prompt mode: capture keystrokes into the pattern buffer.
        # Takes priority so digits go into the pattern, not the count buffer.
        if state.prompt_active:
            if ch in (10, 13, curses.KEY_ENTER):
                state.submit_prompt()
            elif ch in (27, 7):  # Esc, ^G
                state.cancel_prompt()
            elif ch in (127, 8, curses.KEY_BACKSPACE):
                state.backspace_prompt()
            elif 32 <= ch <= 126:
                state.append_prompt(chr(ch))
            continue

        if state.follow:
            # Follow mode: only the cancel-follow keys (^C, ^X, F) work.
            # `q` flashes a hint so it doesn't appear inert; other keys are silent.
            if ch in (3, 24, ord("F")):
                state.toggle_follow()
            elif ch == ord("q"):
                state.set_flash("(^X cancels follow)")
            continue

        # Less-style numeric prefix: digits accumulate, next movement consumes.
        # Esc clears the pending count without firing anything.
        if ord("0") <= ch <= ord("9"):
            count_buffer += chr(ch)
            continue
        if ch == 27 and count_buffer:  # Esc cancels pending count
            count_buffer = ""
            continue

        # Consume any pending count once for this command.
        n_raw = int(count_buffer) if count_buffer else 0
        count_buffer = ""
        n: int | None = n_raw if n_raw > 0 else None

        # Help overlay: pageable with the same movement keys; q / Esc / h closes.
        if state.help_active:
            if ch in (ord("q"), 27, ord("h"), ord("H")):
                state.toggle_help()
            elif ch in _KEYS_GOTO_START:
                state.help_goto_start()
            elif ch in _KEYS_GOTO_END:
                state.help_goto_end()
            else:
                _dispatch_scroll(
                    ch, n,
                    down=state.help_scroll_down,
                    up=state.help_scroll_up,
                    page_down=state.help_page_down,
                    page_up=state.help_page_up,
                    half_down=lambda: state.help_scroll_down(max(1, state.view_height // 2)),
                    half_up=lambda: state.help_scroll_up(max(1, state.view_height // 2)),
                )
            continue

        if ch == ord("q"):
            return 0
        elif ch == 3:  # ^C — hint instead of quitting
            state.set_flash("(q to quit)")
        elif ch in _KEYS_GOTO_START:
            # `Ng` jumps to line N; bare `g` goes to start.
            state.goto_line(n) if n else state.goto_start()
        elif ch in _KEYS_GOTO_END:
            # `NG` jumps to line N; bare `G` goes to end.
            state.goto_line(n) if n else state.goto_end()
        elif _dispatch_scroll(
            ch, n,
            down=state.scroll_down,
            up=state.scroll_up,
            page_down=state.page_down,
            page_up=state.page_up,
            half_down=state.half_page_down,
            half_up=state.half_page_up,
        ):
            pass
        elif ch == ord("F"):
            state.toggle_follow()
        elif ch == ord("/"):
            state.start_prompt("forward")
        elif ch == ord("?"):
            state.start_prompt("backward")
        elif ch == ord("n"):
            state.search_repeat(reverse=False)
        elif ch == ord("N"):
            state.search_repeat(reverse=True)
        elif ch in (ord("h"), ord("H")):
            state.toggle_help()
        elif ch in (ord("r"), 12, 18):  # r, ^L, ^R — repaint
            try:
                stdscr.redrawwin()
            except curses.error:
                pass


def _drain_source(source: PagerSource, state: PagerState) -> None:
    """Pull everything pending from the source queue into the state."""
    new_lines: list[Line] = []
    while True:
        try:
            item = source.queue.get_nowait()
        except queue.Empty:
            break
        if isinstance(item, Line):
            new_lines.append(item)
        else:  # SourceEvent
            if item.kind == "hung":
                state.set_state_badge("hung")
            elif item.kind == "exit":
                state.set_state_badge(_exit_badge(item.info))
            elif item.kind == "removed":
                state.set_state_badge("REMOVED")
                state.set_flash("removed")
    if new_lines:
        state.feed_lines(new_lines)


def _render(
    stdscr,
    state: PagerState,
    info: SessionInfo,
    *,
    strip: bool,
    count_buffer: str = "",
) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    content_rows = max(1, h - 1)

    if state.help_active:
        _render_help(stdscr, w, content_rows, state.help_view_top)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        help_status = "(press q, h, or Esc to close help — j/k/space/b to scroll)"
        try:
            stdscr.addstr(h - 1, 0, help_status[:w].ljust(w), curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()
        return

    visible = state.visible()[:content_rows]
    for i in range(content_rows):
        if i < len(visible):
            text = visible[i].text
            if strip:
                text = strip_ansi(text)
            s = text.rstrip(b"\r\n").decode("utf-8", errors="replace")
            s = s[: max(0, w - 1)]
        else:
            # Past-end placeholder, matches less/vim.
            s = "~"
        try:
            stdscr.addstr(i, 0, s)
        except curses.error:
            pass

    # Highlight visible search matches (after content is painted so chgat
    # overlays them in place).
    if state.search_pattern:
        for row, c0, c1 in state.visible_matches():
            if row >= content_rows:
                continue
            n = min(c1, w - 1) - c0
            if n <= 0:
                continue
            try:
                stdscr.chgat(row, c0, n, curses.A_REVERSE)
            except curses.error:
                pass

    # Bottom row: prompt during search entry, otherwise the status bar.
    if state.prompt_active:
        prefix = "/" if state.prompt_direction == "forward" else "?"
        text = (prefix + state.prompt_buffer)[: max(0, w - 1)]
        try:
            stdscr.addstr(h - 1, 0, text)
            curses.curs_set(1)
            stdscr.move(h - 1, min(len(text), w - 1))
        except curses.error:
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        status = state.status_text(info.id)
        if count_buffer:
            # Echo the pending numeric prefix so the user sees "5" before they
            # decide which movement key to press.
            status = f"{status}  :{count_buffer}"
        padded = status[:w].ljust(w)
        try:
            stdscr.addstr(h - 1, 0, padded, curses.A_REVERSE)
        except curses.error:
            # addstr to the bottom-right cell errors on cursor advance; harmless.
            pass
    stdscr.refresh()
