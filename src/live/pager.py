"""Interactive curses pager for `live less`.

Layered for testability:
  - `PagerState`: pure in-memory view model (scroll, seen benchmark, follow flag).
  - `load_lines`: snapshot the session into `Line` records + partial tail.
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
import unicodedata
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import TypeVar

from .ansi import (
    DEFAULT_STYLE,
    Style,
    parse_spans,
    strip_ansi_str,
    to_base16,
)
from .config import Config
from .format import LOCK_NAME, idx_name, list_segments, stream_name
from .lock import probe_held
from .reader import cat_all, load_stream_view, write_stdout
from .session import SessionInfo, session_info
from .watcher import new_watcher

# ----- data model -----


@dataclass(frozen=True)
class Line:
    """One recorded line. `text` includes its trailing newline.

    `end_byte` is the lifetime byte offset just past the end of this line,
    used by the pager status bar to show byte position within the rendered
    view (and matching `tail -c` cursors).
    """

    text: bytes
    n: int
    t: float
    end_byte: int


@dataclass(frozen=True)
class PartialTail:
    """Unterminated bytes past the last indexed line (no idx record yet).

    `text` is empty when there is no partial tail; `end_byte` is the lifetime
    offset just past the tail (the stream tip, matching `tail -c` cursors).
    """

    text: bytes
    end_byte: int


@dataclass
class _ScanCursor:
    """Walk position shared between the initial snapshot and incremental drains.

    `next_n` is the next line number to consume; `next_byte` is the lifetime
    offset where that line starts. Advancing it lets the next call skip every
    segment wholly below `next_byte`.
    """

    next_n: int = 0
    next_byte: int = 0


def _scan_from(
    session_dir: Path, cursor: _ScanCursor
) -> tuple[list[Line], PartialTail]:
    """Return (new complete Lines past `cursor`, current partial tail),
    advancing `cursor`.

    Lines are sliced by idx byte offsets, so a line spanning segments comes
    back whole; one whose head was retained away between drains comes back
    truncated rather than dropped. The partial tail is whatever sits past the
    last indexed line (empty `text` when the stream ends at a line boundary).
    """
    view = load_stream_view(session_dir, from_byte=cursor.next_byte)
    out: list[Line] = []
    for i, (n, t, b) in enumerate(view.records):
        if cursor.next_n and n < cursor.next_n:
            continue
        if i + 1 < len(view.records):
            end = view.records[i + 1][2]
        else:
            end = view.last_end
        out.append(
            Line(text=view.slice(max(b, view.base), end), n=n, t=t, end_byte=end)
        )
        cursor.next_n = n + 1
        cursor.next_byte = end
    tail_start = max(view.last_end, cursor.next_byte)
    partial = PartialTail(text=view.slice(tail_start, view.tip), end_byte=view.tip)
    return out, partial


def load_lines(session_dir: Path) -> tuple[list[Line], PartialTail]:
    """Snapshot a session: every complete (indexed) line plus the partial tail."""
    return _scan_from(session_dir, _ScanCursor())


# ----- display text -----


def _display_text(line: Line) -> str:
    """Decoded line text, escapes intact, line ending trimmed.

    Single decode point: render, styling, and search columns only agree if
    they decode identically (order matters around invalid UTF-8).
    """
    return line.text.decode("utf-8", errors="replace").rstrip("\r\n")


def _cell_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _cells(text: str) -> int:
    """Display width of `text` in terminal cells."""
    if text.isascii():
        return len(text)
    return sum(_cell_width(ch) for ch in text)


_TAB_STOP = 8


def _plain(text: str) -> bool:
    """True when `text` expands to itself, one cell per char."""
    return text.isascii() and text.isprintable()


def _expand_pieces(text: str, col: int) -> tuple[list[str], int]:
    """Per-char expansion pieces starting at cell `col`, and the end column.

    Single source for `_expand` and `_expand_offsets`, so painted text and
    highlight offsets can't drift.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\t":
            pad = _TAB_STOP - col % _TAB_STOP
            out.append(" " * pad)
            col += pad
        elif ch < " " or ch == "\x7f":
            out.append("^" + chr((ord(ch) + 64) & 0x7F))  # 0x7f -> ^?
            col += 2
        elif "\x80" <= ch <= "\x9f":
            # C1 would reach the terminal as commands if painted raw.
            out.append(f"<{ord(ch):02X}>")
            col += 4
        elif unicodedata.category(ch) == "Cf":
            # Format chars render zero-width (drifting tab stops and
            # highlight cells) or reorder the line (BiDi overrides).
            piece = f"<U+{ord(ch):04X}>"
            out.append(piece)
            col += len(piece)
        else:
            out.append(ch)
            col += _cell_width(ch)
    return out, col


def _expand(text: str, col: int) -> tuple[str, int]:
    """Expand tabs and encode controls less-style (^G, <85>, <U+200B>).

    Starts at cell `col`; leaves only position-independent widths, so
    highlight columns stay exact. Returns the text and the end column.
    """
    if _plain(text):
        return text, col + len(text)
    pieces, col = _expand_pieces(text, col)
    return "".join(pieces), col


def _expand_offsets(text: str) -> list[int]:
    """Char offset into `_expand(text, 0)[0]` for each offset into `text`.

    `len(text) + 1` entries; the last is the expanded length.
    """
    if _plain(text):
        return list(range(len(text) + 1))
    pieces, _ = _expand_pieces(text, 0)
    offs = [0]
    for piece in pieces:
        offs.append(offs[-1] + len(piece))
    return offs


def _clip_cells(text: str, budget: int) -> tuple[str, int]:
    """Longest prefix of `text` that fits in `budget` cells, and its width."""
    if text.isascii():
        clipped = text[: max(0, budget)]
        return clipped, len(clipped)
    cells = 0
    for idx, ch in enumerate(text):
        cw = _cell_width(ch)
        if cells + cw > budget:
            return text[:idx], cells
        cells += cw
    return text, cells


_V = TypeVar("_V")


def _evict_half(cache: dict[int, _V]) -> None:
    """Drop the oldest half (insertion order) so the hot tail stays warm."""
    for k in list(cache)[: len(cache) // 2]:
        del cache[k]


# ----- pure view state -----


@dataclass
class PagerState:
    """Pager view model. No I/O. All transitions are method calls.

    `seen` is the highest line number the user has had visible. `new_count` is
    `lines[-1].n - seen` — the "+K new" counter on the status line.

    `partial` is the unterminated tail, displayed as one extra trailing line.
    It has no idx record yet, so it is presented under its *predicted* number
    `lines[-1].n + 1` (line numbers are consecutive, so that is the number it
    takes once its newline arrives) with the previous line's timestamp. Unlike
    `lines` it mutates in place, so per-index memos never hold it.
    """

    lines: list[Line] = field(default_factory=list)
    partial: Line | None = None
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
    prompt_kind: str = "search"  # "search" | "line"
    prompt_direction: str = "forward"
    prompt_buffer: str = ""
    help_active: bool = False
    help_view_top: int = 0  # scroll position within the help overlay
    # visible_matches() memo, keyed by
    # (pattern, view_top, view_height, len(lines), partial end_byte)
    _match_cache: (
        tuple[tuple[str, int, int, int, int], list[tuple[int, int, int]]] | None
    ) = field(default=None, repr=False, compare=False)
    # _decode() memo; the buffer is append-only, so entries never go stale.
    _decode_cache: dict[int, str] = field(
        default_factory=dict, repr=False, compare=False
    )
    # Per-line match memo: line index -> (pattern, expanded match spans).
    _line_matches: dict[int, tuple[str, list[tuple[int, int]]]] = field(
        default_factory=dict, repr=False, compare=False
    )

    _MEMO_MAX = 4096  # per-cache entry cap; bounds memory on huge buffers

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
        self._reconcile_partial()
        if self.follow:
            self.goto_end()
        else:
            self._update_seen()

    def set_partial(self, tail: PartialTail | None) -> None:
        """Install, replace, or clear the partial-tail display line."""
        old = self.partial
        if tail is None or not tail.text:
            self.partial = None
        else:
            last = self.lines[-1] if self.lines else None
            self.partial = Line(
                text=tail.text,
                n=last.n + 1 if last else 1,
                t=last.t if last else 0.0,
                end_byte=tail.end_byte,
            )
        if self.partial == old:
            return
        if self.partial is None:
            self.view_top = min(self.view_top, self._max_view_top())
        if self.follow:
            self.goto_end()
        else:
            self._update_seen()

    def _reconcile_partial(self) -> None:
        """Keep the partial consistent after commits: drop it once committed
        bytes cover it (no duplicate row while the source's cleared-tail
        message is still in flight), else renumber it past the new last line."""
        if self.partial is None or not self.lines:
            return
        last = self.lines[-1]
        if self.partial.end_byte <= last.end_byte:
            self.partial = None
        elif self.partial.n != last.n + 1:
            self.partial = Line(
                text=self.partial.text,
                n=last.n + 1,
                t=last.t,
                end_byte=self.partial.end_byte,
            )

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
        count = self._line_count()
        if not count:
            return
        target = count - 1
        for i in range(count):
            if self._line_at(i).n >= n:
                target = i
                break
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
        self.prompt_kind = "search"
        self.prompt_direction = direction
        self.prompt_buffer = ""
        self.follow = False  # entering a search disables auto-follow

    def start_line_prompt(self) -> None:
        """Enter line-number entry mode for `:` (`:42` jumps to line 42)."""
        self.prompt_active = True
        self.prompt_kind = "line"
        self.prompt_buffer = ""
        self.follow = False

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
        """Execute the entered prompt: run the search, or jump to the line.
        Returns True on a search match or a line jump."""
        buf = self.prompt_buffer
        direction = self.prompt_direction
        self.prompt_active = False
        self.prompt_buffer = ""
        if not buf:
            return False
        if self.prompt_kind == "line":
            try:
                n = int(buf)
            except ValueError:
                self.set_flash("(invalid line number)")
                return False
            self.goto_line(n)
            return True
        return self.search(buf, direction)

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

    def _compile(
        self, pattern: str, *, silent: bool = False
    ) -> "re.Pattern[str] | None":
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
        if not self._line_count():
            self.set_flash("(pattern not found)")
            return False

        skip = self.search_last_match == self.view_top
        if direction == "forward":
            start = self.view_top + (1 if skip else 0)
            rng = range(start, self._line_count())
        else:
            start = self.view_top - (1 if skip else 0)
            rng = range(start, -1, -1)

        for i in rng:
            if regex.search(self._decode(i)):
                self.search_last_match = i
                # Scroll match to top of viewport (clamped at the tail).
                self.view_top = max(0, min(self._max_view_top(), i))
                self._update_seen()
                return True
        self.set_flash("(pattern not found)")
        return False

    def visible_matches(self) -> list[tuple[int, int, int]]:
        """Match spans for the renderer's highlight overlay.

        `(row, col_start, col_end)`: viewport row and char offsets into the
        expanded display text. Rows ascend; spans ascend within a row.
        """
        if not self.search_pattern:
            return []
        # Called every frame; inputs change only on search/scroll/resize/feed.
        # The partial's end_byte grows with its text, keying its mutations.
        key = (
            self.search_pattern,
            self.view_top,
            self.view_height,
            len(self.lines),
            self.partial.end_byte if self.partial else -1,
        )
        if self._match_cache is not None and self._match_cache[0] == key:
            return self._match_cache[1]
        regex = self._compile(self.search_pattern, silent=True)
        if regex is None:
            return []
        out: list[tuple[int, int, int]] = []
        rows = min(self.view_height, self._line_count() - self.view_top)
        for row in range(rows):
            out.extend(
                (row, c0, c1)
                for c0, c1 in self._line_matches_at(regex, self.view_top + row)
            )
        self._match_cache = (key, out)
        return out

    def _line_matches_at(
        self, regex: "re.Pattern[str]", i: int
    ) -> list[tuple[int, int]]:
        """Expanded-text match spans of line `i`, memoized per line.

        Scrolling shifts the viewport over mostly-unchanged lines; the memo
        keeps those from re-running the regex and offset expansion. The
        partial row mutates in place, so it is computed fresh every time.
        """
        committed = i < len(self.lines)
        if committed:
            cached = self._line_matches.get(i)
            if cached is not None and cached[0] == regex.pattern:
                return cached[1]
        raw = self._decode(i)
        spans = [
            (m.start(), m.end())
            for m in regex.finditer(raw)
            if m.end() > m.start()  # skip zero-width; nothing to paint
        ]
        if spans:
            # Map raw-text match offsets onto the painted (expanded) text.
            offs = _expand_offsets(raw)
            spans = [(offs[s], offs[e]) for s, e in spans]
        if committed:
            if len(self._line_matches) >= self._MEMO_MAX:
                _evict_half(self._line_matches)
            self._line_matches[i] = (regex.pattern, spans)
        return spans

    def _decode(self, i: int) -> str:
        """Search text of line `i`: escapes stripped, tabs/controls raw.

        Raw text keeps `\\t`-style patterns matchable; `visible_matches`
        maps match offsets onto the painted text. The partial row is never
        cached — it mutates in place at a fixed index.
        """
        if i >= len(self.lines):
            return strip_ansi_str(_display_text(self._line_at(i)))
        cached = self._decode_cache.get(i)
        if cached is None:
            cached = strip_ansi_str(_display_text(self.lines[i]))
            if len(self._decode_cache) >= self._MEMO_MAX:
                _evict_half(self._decode_cache)
            self._decode_cache[i] = cached
        return cached

    # ----- derived state -----

    def visible(self) -> list[Line]:
        out = self.lines[self.view_top : self.view_top + self.view_height]
        if (
            self.partial is not None
            and len(out) < self.view_height
            and self.view_top <= len(self.lines)
        ):
            out.append(self.partial)
        return out

    def view_bottom_line(self) -> Line | None:
        vis = self.visible()
        return vis[-1] if vis else None

    def new_count(self) -> int:
        if not self.lines:
            return 0
        return max(0, self.lines[-1].n - self.seen)

    def status_text(self, session_id: str) -> str:
        bot = self.view_bottom_line()
        # The partial counts as the (predicted) last line for positions.
        last = self.partial or (self.lines[-1] if self.lines else None)
        if bot is None or last is None:
            at_line = "0/0"
            at_byte = "0/0"
            at_time = "0.000/0.000"
        else:
            at_line = f"{bot.n}/{last.n}"
            at_byte = f"{bot.end_byte}/{last.end_byte}"
            at_time = f"{bot.t:.3f}/{last.t:.3f}"
        parts = [
            f"id={session_id[:8]} at-line={at_line} at-byte={at_byte} at-time={at_time}"
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

    def _line_count(self) -> int:
        """Display rows in the buffer: committed lines plus the partial tail."""
        return len(self.lines) + (1 if self.partial is not None else 0)

    def _line_at(self, i: int) -> Line:
        """Display line at buffer index `i`; index `len(lines)` is the partial."""
        return self.lines[i] if i < len(self.lines) else self.partial

    def _max_view_top(self) -> int:
        # Last line at the TOP of the screen (so any line — including the very
        # last — can be scrolled into the top row by search or movement). Rows
        # below the buffer are filled with `~` placeholders at render time.
        return max(0, self._line_count() - 1)

    def _natural_max_view_top(self) -> int:
        # Last line at the BOTTOM of the screen — the furthest down movement
        # keys are allowed to scroll without leaving `~` rows.
        return max(0, self._line_count() - self.view_height)

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
    """Background thread that watches a session and pushes new `Line`s,
    `PartialTail` updates, and lifecycle `SourceEvent`s onto a thread-safe queue.

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
        self.queue: queue.Queue[Line | PartialTail | SourceEvent] = queue.Queue()
        self._last_partial: tuple[bytes, int] | None = None
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
        watched_seg: int | None = None
        watched_paths: list[Path] = []

        def _watch_segment(seg: int) -> None:
            # Idx (committed lines) plus stream (partial-tail bytes), so the
            # pager's partial row updates with GNU-tail latency.
            nonlocal watched_seg
            for old in watched_paths:
                watcher.remove_path(old)
            watched_paths.clear()
            for p in (
                self.session_dir / idx_name(seg),
                self.session_dir / stream_name(seg),
            ):
                try:
                    watcher.add_path(p)
                    watched_paths.append(p)
                except OSError:
                    pass
            watched_seg = seg

        try:
            try:
                watcher.add_path(self.session_dir)
            except OSError:
                pass
            segs = list_segments(self.session_dir)
            if segs:
                _watch_segment(segs[-1])

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

                if segs[-1] != watched_seg:
                    _watch_segment(segs[-1])

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
        """Push newly indexed lines (and partial-tail changes) onto the queue,
        resuming where we left off.

        Cost is O(bytes past the scan cursor) per wakeup — segments wholly
        below `self._scan.next_byte` are not re-read.
        """
        emitted = False
        lines, partial = _scan_from(self.session_dir, self._scan)
        for line in lines:
            if line.n > self.cursor:
                self.queue.put(line)
                self.cursor = line.n
                emitted = True
        # Lines go first so the consumer numbers the partial off fresh commits.
        key = (partial.text, partial.end_byte)
        if key != self._last_partial:
            self.queue.put(partial)
            self._last_partial = key
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


def _utf8_need(b0: int) -> int:
    """Total UTF-8 sequence length implied by lead byte `b0` (0 = invalid lead)."""
    if b0 < 0x80:
        return 1
    if 0xC2 <= b0 <= 0xDF:
        return 2
    if 0xE0 <= b0 <= 0xEF:
        return 3
    if 0xF0 <= b0 <= 0xF4:
        return 4
    return 0


def _feed_prompt_byte(pending: bytes, ch: int) -> tuple[bytes, str]:
    """Incremental UTF-8 decode of one getch byte for the search prompt.

    Returns `(pending', text)`: `text` is empty while a multibyte sequence is
    incomplete; malformed input is dropped (a byte that breaks a pending
    sequence restarts decoding at that byte).
    """
    if not 0 <= ch <= 0xFF:
        return b"", ""
    buf = pending + bytes([ch])
    need = _utf8_need(buf[0])
    if need == 0:
        return b"", ""
    if len(buf) < need:
        return buf, ""
    try:
        return b"", buf.decode("utf-8")
    except UnicodeDecodeError:
        return _feed_prompt_byte(b"", ch) if pending else (b"", "")


# Less-style key sets. Shared between main-mode and help-overlay dispatch.
# `\n` and `\r` cover Enter on different terminals; the digits/^N/^P/etc are
# the less defaults from `less --help`.
_KEYS_LINE_DOWN = frozenset(
    {ord("e"), 5, ord("j"), 14, ord("\n"), ord("\r"), curses.KEY_DOWN}
)
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
    "    :N CR              Goto line N",
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


class _AttrMap:
    """Translates a `Style` into a curses attribute.

    Color pairs are allocated lazily per (fg, bg) combo and capped by
    COLOR_PAIRS; combos past the cap render with text attributes only.
    Palette indices beyond the terminal's COLORS are approximated down;
    bright foregrounds folded to 8 colors keep their brightness as A_BOLD.
    """

    def __init__(self) -> None:
        self._pairs: dict[tuple[int, int], int] = {}
        self._next_pair = 1
        self._attrs: dict[Style, int] = {}

    def attr(self, style: Style) -> int:
        cached = self._attrs.get(style)
        if cached is None:
            cached = self._attrs[style] = self._compute(style)
        return cached

    def _compute(self, style: Style) -> int:
        a = 0
        if style.bold:
            a |= curses.A_BOLD
        if style.dim:
            a |= curses.A_DIM
        if style.italic:
            a |= getattr(curses, "A_ITALIC", 0)
        if style.underline:
            a |= curses.A_UNDERLINE
        if style.blink:
            a |= curses.A_BLINK
        if style.reverse:
            a |= curses.A_REVERSE
        if not curses.has_colors():
            return a
        fg, fg_folded = self._fit(style.fg)
        bg, _ = self._fit(style.bg)
        if fg_folded:  # bold only affects the foreground; bg brightness is lost
            a |= curses.A_BOLD
        if (fg, bg) != (-1, -1):
            a |= curses.color_pair(self._pair(fg, bg))
        return a

    @staticmethod
    def _fit(c: int) -> tuple[int, bool]:
        """Nearest supported color index, plus whether a bright color was
        folded to its dim base (callers compensate with A_BOLD)."""
        if c < 0:
            return -1, False
        if c < curses.COLORS:
            return c, False
        c16 = c if c < 16 else to_base16(c)
        if c16 < curses.COLORS:
            return c16, False
        if curses.COLORS >= 8:
            return c16 % 8, c16 >= 8
        return -1, False

    def _pair(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        cached = self._pairs.get(key)
        if cached is not None:
            return cached
        pair = self._next_pair
        if pair >= curses.COLOR_PAIRS:
            self._pairs[key] = 0
            return 0
        try:
            curses.init_pair(pair, fg, bg)
        except curses.error:
            self._pairs[key] = 0
            return 0
        self._next_pair += 1
        self._pairs[key] = pair
        return pair


def _expanded_spans(text: str, start: Style) -> list[tuple[str, Style]]:
    """Parse `text` into styled spans with tabs/controls expanded.

    Tab stops depend on the running cell column across chunks, so expansion
    happens here, span by span.
    """
    parsed, _ = parse_spans(text, start)
    col = 0
    out: list[tuple[str, Style]] = []
    for chunk, style in parsed:
        chunk, col = _expand(chunk, col)
        out.append((chunk, style))
    return out


class _LineStyleCache:
    """Start-of-line styles and parsed spans for the (append-only) buffer.

    SGR state persists across newlines, so styles carry over until reset.
    Start styles extend incrementally as lines arrive; spans are memoized
    so an idle render loop repaints from cache.
    """

    _SPANS_MAX = 4096  # bounds memory when scrubbing through huge buffers

    def __init__(self) -> None:
        self._starts: list[Style] = []
        self._end = DEFAULT_STYLE
        self._spans: dict[int, list[tuple[str, Style]]] = {}

    def start_style(self, lines: list[Line], i: int) -> Style:
        while len(self._starts) <= i:
            j = len(self._starts)
            self._starts.append(self._end)
            # Escape-free lines can't change SGR state; skip the parse.
            if b"\x1b" in lines[j].text:
                _, self._end = parse_spans(_display_text(lines[j]), self._end)
        return self._starts[i]

    def tail_style(self, lines: list[Line]) -> Style:
        """SGR carry-over state after the last line — the start style for the
        partial tail."""
        if lines:
            self.start_style(lines, len(lines) - 1)  # extend through the last
        return self._end

    def spans(
        self, lines: list[Line], i: int, *, styled: bool = True
    ) -> list[tuple[str, Style]]:
        """Parsed spans of line `i`. `styled=False` skips carry-over styling
        (the chunks are identical either way)."""
        cached = self._spans.get(i)
        if cached is None:
            start = self.start_style(lines, i) if styled else DEFAULT_STYLE
            cached = _expanded_spans(_display_text(lines[i]), start)
            if len(self._spans) >= self._SPANS_MAX:
                self._spans.clear()
            self._spans[i] = cached
        return cached


def run_pager(info: SessionInfo, cfg: Config, *, strip: bool) -> int:
    """Open the pager on `info`. Falls back to cat when stdout isn't a TTY."""
    if not sys.stdout.isatty():
        return _cat_fallback(info, strip=strip)
    scan = _ScanCursor()
    lines, partial = _scan_from(info.path, scan)
    state = PagerState(lines=lines, state_badge=info.status)
    state.set_partial(partial)
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
    write_stdout(cat_all(info.path), strip)
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
    prompt_pending = b""  # incomplete UTF-8 sequence from the search prompt
    attrs = _AttrMap()
    styles = _LineStyleCache()

    while True:
        h, _w = stdscr.getmaxyx()
        state.resize(max(1, h - 1))
        _drain_source(source, state)
        _render(
            stdscr,
            state,
            info,
            strip=strip,
            count_buffer=count_buffer,
            attrs=attrs,
            styles=styles,
        )

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
                prompt_pending = b""
            elif ch in (27, 7):  # Esc, ^G
                state.cancel_prompt()
                prompt_pending = b""
            elif ch in (127, 8, curses.KEY_BACKSPACE):
                state.backspace_prompt()
                prompt_pending = b""
            elif 32 <= ch <= 255:
                # Multibyte UTF-8 arrives as individual getch bytes.
                prompt_pending, text = _feed_prompt_byte(prompt_pending, ch)
                if text:
                    state.append_prompt(text)
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
                    ch,
                    n,
                    down=state.help_scroll_down,
                    up=state.help_scroll_up,
                    page_down=state.help_page_down,
                    page_up=state.help_page_up,
                    half_down=lambda: state.help_scroll_down(
                        max(1, state.view_height // 2)
                    ),
                    half_up=lambda: state.help_scroll_up(
                        max(1, state.view_height // 2)
                    ),
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
            ch,
            n,
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
        elif ch == ord(":"):
            state.start_line_prompt()
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
    partial: PartialTail | None = None
    while True:
        try:
            item = source.queue.get_nowait()
        except queue.Empty:
            break
        if isinstance(item, Line):
            new_lines.append(item)
        elif isinstance(item, PartialTail):
            partial = item  # latest snapshot wins
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
    # After feed_lines, so the partial is numbered against the new last line.
    if partial is not None:
        state.set_partial(partial)


def _row_spans(
    state: PagerState, styles: _LineStyleCache, i: int, color: bool
) -> list[tuple[str, Style]]:
    """Painted spans for buffer row `i`. The partial-tail row (index
    `len(lines)`) mutates in place, so it bypasses the style cache."""
    if i < len(state.lines):
        return styles.spans(state.lines, i, styled=color)
    start = styles.tail_style(state.lines) if color else DEFAULT_STYLE
    return _expanded_spans(_display_text(state._line_at(i)), start)


def _safe_addstr(stdscr, y: int, x: int, s: str, attr: int = 0) -> None:
    # Writes touching the bottom-right cell raise after painting; ignore.
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass


def _render(
    stdscr,
    state: PagerState,
    info: SessionInfo,
    *,
    strip: bool,
    attrs: _AttrMap,
    styles: _LineStyleCache,
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
    color = not strip
    for i in range(content_rows):
        if i >= len(visible):
            # Past-end placeholder, matches less/vim.
            _safe_addstr(stdscr, i, 0, "~")
            continue
        col = 0  # cells, not chars: wide glyphs occupy two
        for chunk, style in _row_spans(state, styles, state.view_top + i, color):
            if col >= w - 1:
                break
            clipped, cells = _clip_cells(chunk, w - 1 - col)
            _safe_addstr(stdscr, i, col, clipped, attrs.attr(style) if color else 0)
            col += cells
            if len(clipped) < len(chunk):
                # Clipped mid-chunk (wide glyph at the edge): later chunks
                # would paint at the unadvanced col, left of their cells.
                break

    # Overlay search highlights; match columns are char offsets into the
    # expanded display text, converted to cells to land on the painted glyphs.
    if state.search_pattern:
        for row, group in groupby(state.visible_matches(), key=lambda m: m[0]):
            if row >= content_rows:
                break  # rows ascend
            text = "".join(
                chunk
                for chunk, _ in _row_spans(state, styles, state.view_top + row, color)
            )
            # Spans ascend within the row: one pass converts offsets to cells.
            pos = 0
            cell = 0
            for _, c0, c1 in group:
                cell += _cells(text[pos:c0])
                start = cell
                cell += _cells(text[c0:c1])
                pos = c1
                n = min(cell - start, max(0, w - 1 - start))
                if n <= 0:
                    continue
                try:
                    stdscr.chgat(row, start, n, curses.A_REVERSE)
                except curses.error:
                    pass

    # Bottom row: prompt during search entry, otherwise the status bar.
    if state.prompt_active:
        if state.prompt_kind == "line":
            prefix = ":"
        else:
            prefix = "/" if state.prompt_direction == "forward" else "?"
        # Clip and place the cursor by cells, not chars (wide glyphs are 2).
        text, cells = _clip_cells(prefix + state.prompt_buffer, max(0, w - 1))
        try:
            stdscr.addstr(h - 1, 0, text)
            curses.curs_set(1)
            stdscr.move(h - 1, min(cells, w - 1))
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
