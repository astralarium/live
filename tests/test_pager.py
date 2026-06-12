"""`PagerState` — pure view-state transitions for `live less`."""

from __future__ import annotations

import queue
import shutil
import time
from pathlib import Path

from live.config import Config
from live.format import IDX_HEADER, IDX_RECORD
from live.pager import (
    Line,
    PagerSource,
    PagerState,
    PartialTail,
    SourceEvent,
    _drain_source,
    _feed_prompt_byte,
    _scan_from,
    _ScanCursor,
)


def _mk(n: int, text: str = "x") -> Line:
    """Synthetic Line for state tests. end_byte assumes lines are produced in
    order with this text, so it equals `n * len(body)`."""
    body = (text + "\n").encode()
    return Line(text=body, n=n, t=float(n), end_byte=n * len(body))


# ----- view_bottom + seen + counter -----


def test_initial_load_marks_all_preloaded_lines_as_seen() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    # View shows lines 1-3, but everything preloaded counts as seen.
    assert s.view_bottom_line().n == 3
    assert s.seen == 10
    assert s.new_count() == 0


def test_scroll_does_not_decrease_seen() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    s.scroll_down(2)
    assert s.view_bottom_line().n == 5
    assert s.seen == 10
    assert s.new_count() == 0


def test_feed_after_load_starts_counter() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    s.feed_lines([_mk(11), _mk(12)])
    # View hasn't moved; new lines below it tick the counter.
    assert s.new_count() == 2


def test_feed_within_viewport_advances_seen() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(10)  # viewport bigger than buffer
    s.feed_lines([_mk(2), _mk(3)])
    # New lines are immediately visible -> seen advances, counter stays 0.
    assert s.view_bottom_line().n == 3
    assert s.seen == 3
    assert s.new_count() == 0


def test_scroll_up_then_feed_starts_counter() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    s.goto_end()
    s.scroll_up(5)
    s.feed_lines([_mk(11), _mk(12)])
    assert s.new_count() == 2


def test_follow_mode_holds_counter_at_zero() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 6)], state_badge="running")
    s.resize(2)
    s.toggle_follow()
    assert s.follow is True
    s.feed_lines([_mk(6), _mk(7)])
    assert s.view_bottom_line().n == 7
    assert s.new_count() == 0


def test_follow_blocked_when_session_not_running() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="exited(code=0)")
    s.resize(5)
    s.toggle_follow()
    assert s.follow is False
    assert "not running" in s.flash_msg


def test_state_badge_change_off_running_disables_follow() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="running")
    s.resize(5)
    s.toggle_follow()
    assert s.follow is True
    s.set_state_badge("exited(code=0)")
    assert s.follow is False


def test_goto_end_after_feed_clears_counter() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    s.feed_lines([_mk(11), _mk(12), _mk(13)])
    assert s.new_count() == 3
    s.goto_end()
    assert s.new_count() == 0


# ----- clamping -----


def test_resize_clamps_view_top() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    s.goto_end()  # last line at bottom of 3-row viewport -> view_top = 7
    assert s.view_top == 7
    s.resize(20)  # viewport bigger than buffer; resize snaps to natural max
    assert s.view_top == 0


def test_page_down_clamps_at_natural_max_not_into_tildes() -> None:
    # 4 lines, viewport 3 -> natural max = 1 (last line at bottom row).
    s = PagerState(lines=[_mk(i) for i in range(1, 5)])
    s.resize(3)
    s.page_down()
    s.page_down()
    # view_top stays at natural max; movement keys never push into the `~` zone.
    assert s.view_top == 1


def test_scroll_down_does_not_push_into_tildes() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 5)])
    s.resize(3)
    s.scroll_down(10)  # natural max = 1; would overshoot
    assert s.view_top == 1


def test_goto_line_positions_target_at_top() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 21)])
    s.resize(5)
    s.goto_line(7)
    assert s.view_top == 6  # 0-indexed buffer position of n=7
    assert s.visible()[0].n == 7


def test_help_toggles_and_resets_scroll_on_close() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(5)
    assert s.help_active is False
    s.toggle_help()
    assert s.help_active is True
    s.help_scroll_down(3)
    assert s.help_view_top > 0
    s.toggle_help()
    assert s.help_active is False
    assert s.help_view_top == 0  # reset on close


def test_help_scroll_clamps_at_max() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(5)
    s.toggle_help()
    s.help_scroll_down(10_000)
    assert s.help_view_top == s._help_max_top()
    s.help_scroll_up(10_000)
    assert s.help_view_top == 0


def test_help_goto_end_and_start() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(5)
    s.toggle_help()
    s.help_goto_end()
    assert s.help_view_top == s._help_max_top()
    s.help_goto_start()
    assert s.help_view_top == 0


def test_goto_line_clamps_to_last_line_when_out_of_range() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(5)
    s.goto_line(999)
    # Last line scrolled to top; tail rows are `~`.
    assert s.view_top == 9
    assert s.visible() == [s.lines[9]]


def test_movement_cannot_re_enter_tildes_after_scroll_up() -> None:
    # 10 lines, viewport 3 -> natural max = 7. Search puts last line at top
    # (view_top = 9, past natural). Scrolling up backs us out toward natural,
    # but j cannot push us back in.
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])
    s.resize(3)
    s.view_top = 9  # simulate search-to-tail positioning
    s.scroll_up(2)
    assert s.view_top == 7  # at natural max
    s.scroll_down(1)
    assert s.view_top == 7  # blocked; j cannot re-enter ~


# ----- empty session -----


def test_empty_buffer_status_is_zeros() -> None:
    s = PagerState()
    s.resize(10)
    assert s.view_bottom_line() is None
    assert s.new_count() == 0
    status = s.status_text("abcdef0123")
    assert "at-line=0/0" in status
    assert "at-byte=0/0" in status
    assert "at-time=0.000/0.000" in status
    assert "id=abcdef01" in status


# ----- status text -----


def test_status_trailer_format_and_order() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)], state_badge="running")
    s.resize(3)
    status = s.status_text("deadbeefcafe")
    assert "id=deadbeef" in status
    assert "at-line=3/10" in status
    # _mk uses end_byte = n * len("x\n") = 2n, so byte 3-of-2*10=20.
    assert "at-byte=6/20" in status
    assert "at-time=3.000/10.000" in status
    assert status.endswith("running")
    # Order: id, at-line, at-byte, at-time
    p_id = status.index("id=")
    p_line = status.index("at-line=")
    p_byte = status.index("at-byte=")
    p_time = status.index("at-time=")
    assert p_id < p_line < p_byte < p_time


def test_status_new_counter_only_appears_when_positive() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)], state_badge="running")
    s.resize(3)
    assert "new" not in s.status_text("abc")
    s.feed_lines([_mk(11), _mk(12)])
    assert "[+2 new]" in s.status_text("abc")


def test_status_flash_overrides_state_badge() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="running")
    s.resize(2)
    s.set_flash("removed", duration=10.0)
    status = s.status_text("abc")
    assert status.endswith("removed")
    assert "running" not in status


def test_status_flash_decays_after_duration() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="running")
    s.resize(2)
    # Set an already-expired flash; should fall through to state_badge.
    s.flash_msg = "stale"
    s.flash_until = time.time() - 1
    status = s.status_text("abc")
    assert "stale" not in status
    assert status.endswith("running")


def test_status_follow_badge() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="running")
    s.resize(2)
    s.toggle_follow()
    status = s.status_text("abc")
    assert "[FOLLOW]" in status
    assert "^X" in status  # interrupt-to-abort hint


def test_state_badge_updates_propagate_to_status() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="running")
    s.resize(2)
    s.state_badge = "exited(code=0)"
    status = s.status_text("abc")
    assert status.endswith("exited(code=0)")


def test_removed_flash_overrides_badge_then_decays() -> None:
    s = PagerState(lines=[_mk(1)], state_badge="running")
    s.resize(2)
    s.state_badge = "REMOVED"
    s.set_flash("removed", duration=10.0)
    assert s.status_text("abc").endswith("removed")
    # Force expiry: badge surfaces.
    s.flash_until = time.time() - 1
    assert s.status_text("abc").endswith("REMOVED")


# ----- search -----


def test_forward_search_scrolls_match_to_top_of_view() -> None:
    s = PagerState(
        lines=[_mk(i, f"line {i}") for i in range(1, 21)]
        + [_mk(21, "needle here")]
        + [_mk(i, f"line {i}") for i in range(22, 31)]
    )
    s.resize(5)
    assert s.search("needle", "forward")
    # Match is at buffer index 20 (line n=21); view_top jumps to that index.
    assert s.search_last_match == 20
    assert s.view_top == 20
    # Match is now the first visible line.
    assert s.visible()[0].n == 21


def test_search_no_match_sets_flash() -> None:
    s = PagerState(lines=[_mk(i, f"line {i}") for i in range(1, 6)])
    s.resize(5)
    assert not s.search("nope", "forward")
    assert "not found" in s.flash_msg


def test_search_repeat_n_advances_past_current_match() -> None:
    s = PagerState(
        lines=[_mk(i, "x") for i in range(1, 6)]
        + [_mk(6, "match1"), _mk(7, "between"), _mk(8, "match2")]
    )
    s.resize(20)
    assert s.search("match", "forward")
    assert s.search_last_match == 5  # match1 at index 5
    assert s.search_repeat()
    assert s.search_last_match == 7  # match2 at index 7


def test_search_repeat_N_reverses_direction() -> None:
    s = PagerState(lines=[_mk(1, "match1"), _mk(2, "x"), _mk(3, "match2")])
    s.resize(10)
    assert s.search("match", "forward")
    assert s.search_last_match == 0
    assert s.search_repeat()
    assert s.search_last_match == 2
    assert s.search_repeat(reverse=True)
    assert s.search_last_match == 0


def test_movement_resets_search_start_position() -> None:
    """After /pattern, scrolling discards the implicit cursor: `n` searches
    from the new view_top, mirroring less's behavior."""
    s = PagerState(
        lines=[_mk(1, "first"), _mk(2, "match1"), _mk(3, "match2")]
        + [_mk(i, f"tail{i}") for i in range(4, 21)]
    )
    s.resize(5)
    s.search("match", "forward")
    assert s.view_top == 1  # match1 at top
    s.goto_end()  # view_top jumps past all matches
    assert s.view_top > 2  # truly past both matches
    # `n` from the new view_top forward finds nothing.
    assert not s.search_repeat()
    # Going back to the start lets `n` find matches again.
    s.goto_start()
    assert s.search_repeat()
    assert s.view_top == 1  # match1 again


def test_search_can_position_past_natural_max() -> None:
    """A match near EOF scrolls to top even if the natural max would clamp it."""
    s = PagerState(lines=[_mk(i, "x") for i in range(1, 10)] + [_mk(10, "match")])
    s.resize(3)
    assert s.search("match", "forward")
    # Last line scrolled to top of 3-row viewport; rows below are placeholders.
    assert s.view_top == 9
    assert s.visible() == [s.lines[9]]


def test_smart_case_is_insensitive_for_lowercase_pattern() -> None:
    s = PagerState(lines=[_mk(1, "MATCH HERE")])
    s.resize(5)
    assert s.search("match", "forward")


def test_uppercase_pattern_is_case_sensitive() -> None:
    s = PagerState(lines=[_mk(1, "match here")])
    s.resize(5)
    assert not s.search("MATCH", "forward")


def test_bad_regex_sets_flash_and_does_not_change_pattern() -> None:
    s = PagerState(lines=[_mk(1, "anything")])
    s.resize(5)
    assert not s.search("[unclosed", "forward")
    assert "bad regex" in s.flash_msg
    assert s.search_pattern == ""


def test_search_disables_follow() -> None:
    s = PagerState(lines=[_mk(1, "match")])
    s.resize(5)
    s.toggle_follow()
    assert s.follow is True
    s.start_prompt("forward")
    assert s.follow is False


def test_prompt_buffer_lifecycle() -> None:
    s = PagerState(lines=[_mk(1, "match here")])
    s.resize(5)
    s.start_prompt("forward")
    assert s.prompt_active and s.prompt_buffer == ""
    s.append_prompt("m")
    s.append_prompt("a")
    assert s.prompt_buffer == "ma"
    s.backspace_prompt()
    assert s.prompt_buffer == "m"
    s.cancel_prompt()
    assert not s.prompt_active
    assert s.prompt_buffer == ""
    assert s.search_pattern == ""  # cancel doesn't run a search


def test_submit_empty_prompt_is_noop() -> None:
    s = PagerState(lines=[_mk(1, "x")])
    s.resize(5)
    s.start_prompt("forward")
    assert not s.submit_prompt()
    assert s.search_pattern == ""


def test_visible_matches_returns_positions_in_viewport() -> None:
    s = PagerState(
        lines=[
            _mk(1, "skip"),
            _mk(2, "find me twice: find"),
            _mk(3, "find again"),
        ]
    )
    s.resize(10)
    s.search("find", "forward")
    # After search, view_top = 1 (the match line scrolled to top).
    matches = s.visible_matches()
    # 3 matches total: "find me twice", second "find", "find again"
    assert len(matches) == 3
    rows = sorted({row for row, _, _ in matches})
    assert rows == [0, 1]  # row 0 = match line, row 1 = "find again"


# ----- PagerSource integration -----


_CFG = Config(ttl_days=7, max_kb=4096, segment_kb=1024, heartbeat_sec=30)


def _drain_until(source_queue, predicate, *, timeout: float = 5.0):
    """Pop items from a source queue until `predicate(item)` is truthy. Returns
    the matching item, or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            item = source_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if predicate(item):
            return item
    return None


def test_pager_source_emits_removed_event_on_rm(spawn_run, wait_for_session) -> None:
    spawn_run("-n", "rmtest")
    session_dir = wait_for_session()
    source = PagerSource(session_dir, _CFG, initial_cursor=0)
    source.start()
    try:
        first_line = _drain_until(source.queue, lambda i: isinstance(i, Line))
        assert first_line is not None, "PagerSource never emitted any lines"

        shutil.rmtree(session_dir)

        event = _drain_until(
            source.queue,
            lambda i: isinstance(i, SourceEvent) and i.kind == "removed",
        )
        assert event is not None, "PagerSource did not emit 'removed' after rmtree"
    finally:
        source.stop()


def test_pager_source_emits_exit_when_recorder_finishes(
    project: Path, run_live, wait_for_session
) -> None:
    # Quick session that exits on its own.
    run_live(project, "run", "-n", "quick", "--", "sh", "-c", "echo done")
    session_dir = wait_for_session()
    source = PagerSource(session_dir, _CFG, initial_cursor=0)
    source.start()
    try:
        event = _drain_until(
            source.queue,
            lambda i: isinstance(i, SourceEvent) and i.kind == "exit",
        )
        assert event is not None, "PagerSource did not emit 'exit' for finished session"
        assert event.info is not None
        assert event.info.exit_code == 0
    finally:
        source.stop()


# ----- partial tail -----


def test_partial_tail_appears_as_trailing_line() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 4)])
    s.resize(10)
    s.set_partial(PartialTail(text=b"prog 10%", end_byte=100))
    vis = s.visible()
    assert vis[-1].text == b"prog 10%"
    assert vis[-1].n == 4  # predicted: next consecutive line number
    assert s.new_count() == 0  # partial doesn't tick the +K counter


def test_partial_tail_growth_updates_in_place() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(10)
    s.set_partial(PartialTail(text=b"prog 10%", end_byte=10))
    s.set_partial(PartialTail(text=b"prog 10%\rprog 90%", end_byte=19))
    vis = s.visible()
    assert len(vis) == 2  # grew, not duplicated
    assert vis[-1].text == b"prog 10%\rprog 90%"
    assert vis[-1].end_byte == 19


def test_partial_converts_to_indexed_line_without_duplication() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(10)
    s.set_partial(PartialTail(text=b"par", end_byte=5))
    s.feed_lines([Line(text=b"partial\n", n=2, t=2.0, end_byte=10)])
    s.set_partial(PartialTail(text=b"", end_byte=10))
    assert s.partial is None
    assert [line.text for line in s.visible()] == [b"x\n", b"partial\n"]


def test_feed_lines_drops_partial_covered_by_commit() -> None:
    # The committed line covers the partial's bytes before the source's
    # cleared-tail message arrives; no duplicate row in the meantime.
    s = PagerState(lines=[_mk(1)])
    s.resize(10)
    s.set_partial(PartialTail(text=b"par", end_byte=5))
    s.feed_lines([Line(text=b"partial\n", n=2, t=2.0, end_byte=10)])
    assert s.partial is None
    assert len(s.visible()) == 2


def test_partial_renumbers_after_unrelated_commit() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(10)
    s.set_partial(PartialTail(text=b"tail", end_byte=20))
    assert s.partial.n == 2
    # Commit that ends below the partial's bytes: partial stays, renumbered.
    s.feed_lines([Line(text=b"two\n", n=2, t=2.0, end_byte=6)])
    assert s.partial is not None
    assert s.partial.n == 3


def test_partial_only_session_displays_and_converts() -> None:
    s = PagerState()
    s.resize(5)
    s.set_partial(PartialTail(text=b"boot...", end_byte=7))
    assert s.visible()[0].n == 1
    assert s.status_text("abc").startswith("id=abc")
    s.feed_lines([Line(text=b"boot...\n", n=1, t=1.0, end_byte=8)])
    assert s.partial is None
    assert [line.n for line in s.visible()] == [1]


def test_partial_counts_toward_scroll_extent() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 5)])
    s.resize(3)
    s.set_partial(PartialTail(text=b"tail", end_byte=20))
    s.scroll_down(10)
    assert s.view_top == 2  # 5 display rows incl partial; natural max = 2
    assert s.visible()[-1].text == b"tail"


def test_goto_line_reaches_partial() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 5)])
    s.resize(3)
    s.set_partial(PartialTail(text=b"tail", end_byte=20))
    s.goto_line(5)
    assert s.visible()[0].text == b"tail"


def test_partial_clear_clamps_overscrolled_view() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 5)])
    s.resize(3)
    s.set_partial(PartialTail(text=b"tail", end_byte=20))
    s.view_top = 4  # search-style overscroll: partial at top row
    s.set_partial(None)
    assert s.view_top == 3  # clamped back to the last committed line


def test_status_counts_partial_as_predicted_last_line() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 11)])  # end_byte = 2n
    s.resize(3)
    s.set_partial(PartialTail(text=b"tail", end_byte=25))
    status = s.status_text("abc")
    assert "at-line=3/11" in status
    assert "at-byte=6/25" in status
    s.goto_end()
    assert "at-line=11/11" in s.status_text("abc")


# ----- search in partial tail -----


def test_search_matches_inside_partial_tail() -> None:
    s = PagerState(lines=[_mk(i) for i in range(1, 6)])
    s.resize(3)
    s.set_partial(PartialTail(text=b"needle in tail", end_byte=99))
    assert s.search("needle", "forward")
    assert s.view_top == 5  # partial row scrolled to top
    assert s.visible()[0].text == b"needle in tail"
    matches = s.visible_matches()
    assert matches and matches[0] == (0, 0, len("needle"))


def test_partial_growth_refreshes_visible_matches() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(5)
    s.set_partial(PartialTail(text=b"ab", end_byte=10))
    assert s.search("ab", "forward")
    assert len(s.visible_matches()) == 1
    s.set_partial(PartialTail(text=b"abab", end_byte=12))
    assert len(s.visible_matches()) == 2  # memo keyed off the growing tail


def test_search_highlights_fresh_after_partial_conversion() -> None:
    s = PagerState(lines=[_mk(1)])
    s.resize(5)
    s.set_partial(PartialTail(text=b"ab", end_byte=4))
    assert s.search("ab", "forward")
    # Commits with different content at the same buffer index as the partial.
    s.feed_lines([Line(text=b"QQab\n", n=2, t=2.0, end_byte=9)])
    s.set_partial(PartialTail(text=b"", end_byte=9))
    assert s.visible_matches() == [(0, 2, 4)]  # no stale partial-era memo


def test_unicode_pattern_matches_unicode_text() -> None:
    s = PagerState(lines=[_mk(1, "naïve £ output")])
    s.resize(5)
    assert s.search("naïve £", "forward")


def test_unicode_prompt_input_builds_and_submits() -> None:
    s = PagerState(lines=[_mk(1, "naïve output")])
    s.resize(5)
    s.start_prompt("forward")
    pending = b""
    for byte in "naïve".encode("utf-8"):
        pending, text = _feed_prompt_byte(pending, byte)
        if text:
            s.append_prompt(text)
    assert s.prompt_buffer == "naïve"
    assert s.submit_prompt()


# ----- prompt UTF-8 byte decoding -----


def test_feed_prompt_byte_ascii_passthrough() -> None:
    assert _feed_prompt_byte(b"", ord("a")) == (b"", "a")


def test_feed_prompt_byte_two_byte_sequence() -> None:
    pending, text = _feed_prompt_byte(b"", 0xC3)
    assert (pending, text) == (b"\xc3", "")
    assert _feed_prompt_byte(pending, 0xA9) == (b"", "é")


def test_feed_prompt_byte_three_byte_sequence() -> None:
    pending = b""
    out = ""
    for byte in "✓".encode("utf-8"):
        pending, text = _feed_prompt_byte(pending, byte)
        out += text
    assert (pending, out) == (b"", "✓")


def test_feed_prompt_byte_four_byte_emoji() -> None:
    pending = b""
    out = ""
    for byte in "😀".encode("utf-8"):
        pending, text = _feed_prompt_byte(pending, byte)
        out += text
    assert (pending, out) == (b"", "😀")


def test_feed_prompt_byte_invalid_bytes_dropped() -> None:
    assert _feed_prompt_byte(b"", 0xFF) == (b"", "")  # invalid lead
    assert _feed_prompt_byte(b"", 0x80) == (b"", "")  # bare continuation


def test_feed_prompt_byte_broken_sequence_salvages_next_char() -> None:
    # Lead byte followed by ASCII: drop the broken lead, keep the ASCII.
    assert _feed_prompt_byte(b"\xc3", ord("a")) == (b"", "a")


def test_feed_prompt_byte_ignores_curses_function_keys() -> None:
    assert _feed_prompt_byte(b"", 0x10B) == (b"", "")  # KEY_* codes > 0xFF


# ----- _scan_from partial extraction -----


def _write_idx(path: Path, recs: list[tuple[int, float, int]]) -> None:
    """Idx fixture: header (segment start, line start) + (n, t, b) records."""
    buf = IDX_HEADER.pack(0, 0)
    for n, t, b in recs:
        buf += IDX_RECORD.pack(n, t, b)
    path.write_bytes(buf)


def test_scan_from_partial_lifecycle(tmp_path: Path) -> None:
    sess = tmp_path / "sess"
    sess.mkdir()
    stream = sess / "stream.0000.log"
    idx = sess / "lines.0000.idx"

    # Two indexed lines plus an unterminated tail.
    stream.write_bytes(b"one\ntwo\npar")
    _write_idx(idx, [(1, 1.0, 0), (2, 2.0, 4)])
    cur = _ScanCursor()
    lines, partial = _scan_from(sess, cur)
    assert [line.text for line in lines] == [b"one\n", b"two\n"]
    assert partial == PartialTail(text=b"par", end_byte=11)

    # Tail grows; no new indexed lines.
    stream.write_bytes(b"one\ntwo\npartial")
    lines, partial = _scan_from(sess, cur)
    assert lines == []
    assert partial == PartialTail(text=b"partial", end_byte=15)

    # Newline + idx record arrive: tail becomes a committed line and clears.
    stream.write_bytes(b"one\ntwo\npartial\n")
    _write_idx(idx, [(1, 1.0, 0), (2, 2.0, 4), (3, 3.0, 8)])
    lines, partial = _scan_from(sess, cur)
    assert [line.text for line in lines] == [b"partial\n"]
    assert partial == PartialTail(text=b"", end_byte=16)


def test_drain_source_applies_lines_before_partial(tmp_path: Path) -> None:
    # Queue order is lines-then-tail; the drain must feed lines first so the
    # partial is numbered against the freshly committed last line.
    source = PagerSource(tmp_path, _CFG, initial_cursor=0)
    s = PagerState(lines=[_mk(1)])
    s.resize(10)
    source.queue.put(Line(text=b"two\n", n=2, t=2.0, end_byte=8))
    source.queue.put(PartialTail(text=b"tail", end_byte=12))
    _drain_source(source, s)
    assert [line.n for line in s.lines] == [1, 2]
    assert s.partial is not None
    assert s.partial.n == 3
