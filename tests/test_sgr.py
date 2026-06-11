"""`live.ansi` (stripping, SGR parsing, color math) and pager color plumbing."""

from __future__ import annotations

from live.ansi import (
    DEFAULT_STYLE,
    Style,
    parse_spans,
    rgb_to_256,
    strip_ansi,
    strip_ansi_str,
    to_base16,
)
from live.pager import Line, PagerState, _LineStyleCache, _cells, _clip_cells


def _spans(text: str, start: Style = DEFAULT_STYLE):
    spans, _end = parse_spans(text, start)
    return spans


# ----- strip_ansi -----


def test_strip_ansi_csi_color_codes() -> None:
    raw = b"\x1b[31mred\x1b[0m\n\x1b[1;32mbold-green\x1b[0m\n"
    assert strip_ansi(raw) == b"red\nbold-green\n"


def test_strip_ansi_osc_window_title() -> None:
    raw = b"\x1b]0;my title\x07after\n"
    assert strip_ansi(raw) == b"after\n"


def test_strip_ansi_two_byte_escape() -> None:
    # ESC D (Index, 0x1B 0x44) is a 2-byte Fe escape in the @-_ range.
    raw = b"before\x1bDafter\n"
    assert strip_ansi(raw) == b"beforeafter\n"


def test_strip_ansi_passthrough_for_clean_text() -> None:
    assert strip_ansi(b"plain text\n") == b"plain text\n"


# ----- span splitting -----


def test_plain_text_single_default_span() -> None:
    assert _spans("hello") == [("hello", DEFAULT_STYLE)]


def test_basic_color_span() -> None:
    spans = _spans("\x1b[31mred\x1b[0m plain")
    assert spans == [
        ("red", Style(fg=1)),
        (" plain", DEFAULT_STYLE),
    ]


def test_concatenated_chunks_equal_stripped_text() -> None:
    text = "\x1b[1;32mbold-green\x1b[0m and \x1b[44mblue-bg\x1b[49m"
    spans = _spans(text)
    assert "".join(c for c, _ in spans) == "bold-green and blue-bg"


def test_non_sgr_escapes_dropped_without_styling() -> None:
    # Cursor movement (CSI A), OSC title, and a 2-byte sequence.
    text = "\x1b[2Aa\x1b]0;title\x07b\x1bMc"
    assert _spans(text) == [("a", DEFAULT_STYLE), ("b", DEFAULT_STYLE), ("c", DEFAULT_STYLE)]


def test_attributes_accumulate_and_reset() -> None:
    spans = _spans("\x1b[1m\x1b[4m\x1b[31mx\x1b[24my\x1b[0mz")
    assert spans[0] == ("x", Style(fg=1, bold=True, underline=True))
    assert spans[1] == ("y", Style(fg=1, bold=True))
    assert spans[2] == ("z", DEFAULT_STYLE)


def test_bright_and_background_colors() -> None:
    spans = _spans("\x1b[91;107mx")
    assert spans == [("x", Style(fg=9, bg=15))]


def test_256_color_semicolon_and_colon_forms() -> None:
    assert _spans("\x1b[38;5;196mx") == [("x", Style(fg=196))]
    assert _spans("\x1b[38:5:196mx") == [("x", Style(fg=196))]
    assert _spans("\x1b[48;5;21mx") == [("x", Style(bg=21))]


def test_truecolor_maps_to_256_palette() -> None:
    [(_, style)] = _spans("\x1b[38;2;255;0;0mx")
    assert style.fg == rgb_to_256(255, 0, 0)
    [(_, style)] = _spans("\x1b[38:2::255:0:0mx")
    assert style.fg == rgb_to_256(255, 0, 0)


def test_malformed_extended_color_is_skipped() -> None:
    assert _spans("\x1b[38;5mx") == [("x", DEFAULT_STYLE)]
    assert _spans("\x1b[38;5;999mx") == [("x", DEFAULT_STYLE)]


def test_underline_colon_subparams() -> None:
    # ITU T.416 / kitty styled underlines: 4:0 clears, 4:1..5 set.
    assert _spans("\x1b[4:3mx") == [("x", Style(underline=True))]
    assert _spans("\x1b[4:0mx", Style(underline=True)) == [("x", DEFAULT_STYLE)]


def test_unknown_extended_mode_consumes_only_itself() -> None:
    # A 38/48 with an unrecognized mode must not swallow later parameters.
    assert _spans("\x1b[38;4mx") == [("x", Style(underline=True))]


def test_strip_ansi_str_equals_span_chunks() -> None:
    text = "\x1b[31mred\x1b[0m \x1b]0;t\x07plain"
    assert strip_ansi_str(text) == "red plain"
    assert strip_ansi_str(text) == "".join(c for c, _ in _spans(text))


def test_empty_param_means_reset() -> None:
    assert _spans("\x1b[31mx\x1b[my") == [("x", Style(fg=1)), ("y", DEFAULT_STYLE)]


def test_carry_over_between_lines() -> None:
    spans, end = parse_spans("\x1b[33mno reset here")
    assert end == Style(fg=3)
    spans, end = parse_spans("still yellow\x1b[0m done", end)
    assert spans[0] == ("still yellow", Style(fg=3))
    assert end == DEFAULT_STYLE


# ----- color math -----


def test_rgb_to_256_extremes() -> None:
    assert rgb_to_256(0, 0, 0) == 16
    assert rgb_to_256(255, 255, 255) == 231
    assert rgb_to_256(255, 0, 0) == 196
    assert rgb_to_256(0, 255, 0) == 46
    assert rgb_to_256(0, 0, 255) == 21


def test_rgb_to_256_gray_uses_ramp() -> None:
    idx = rgb_to_256(128, 128, 128)
    assert 232 <= idx <= 255


def test_rgb_to_256_gray_rounds_to_nearest_step() -> None:
    assert rgb_to_256(17, 17, 17) == 233  # ramp value 18 beats 8
    assert rgb_to_256(12, 12, 12) == 232  # ramp value 8 beats 18


def test_to_base16_roundtrips_basic_colors() -> None:
    for i in range(16):
        assert to_base16(i) == i


def test_to_base16_approximates_cube_colors() -> None:
    assert to_base16(196) == 9  # pure red -> bright red
    assert to_base16(21) in (4, 12)  # pure blue -> a blue


# ----- pager plumbing -----


def _line(n: int, text: str) -> Line:
    body = text.encode()
    return Line(text=body, n=n, t=float(n), end_byte=n * len(body))


def test_line_style_cache_carries_color_across_lines() -> None:
    lines = [
        _line(1, "\x1b[35mopen magenta\n"),
        _line(2, "still magenta\n"),
        _line(3, "\x1b[0mback to normal\n"),
        _line(4, "normal\n"),
    ]
    cache = _LineStyleCache()
    assert cache.start_style(lines, 0) == DEFAULT_STYLE
    assert cache.start_style(lines, 1) == Style(fg=5)
    assert cache.start_style(lines, 2) == Style(fg=5)
    assert cache.start_style(lines, 3) == DEFAULT_STYLE


def test_search_matches_against_display_text() -> None:
    # Escapes inside the line must not split the pattern or shift columns.
    s = PagerState(lines=[_line(1, "ab \x1b[31mcd\x1b[0m ef\n")])
    s.resize(5)
    s.search_pattern = "cd ef"
    assert s.visible_matches() == [(0, 3, 8)]


def test_search_text_equals_rendered_text_on_invalid_utf8() -> None:
    # An escape interrupting a multi-byte UTF-8 char: search must see the
    # same replacement chars the renderer paints, or highlight columns shift.
    raw = b"\xe2\x82\x1b[31m\xac cd ef\n"
    line = Line(text=raw, n=1, t=1.0, end_byte=len(raw))
    rendered = "".join(c for c, _ in _LineStyleCache().spans([line], 0))
    assert PagerState._decode(line) == rendered


def test_style_cache_skips_escape_free_lines() -> None:
    # Plain lines can't change SGR state; the fast path must preserve carry-over.
    lines = [_line(1, "\x1b[36mopen cyan\n"), _line(2, "plain\n"), _line(3, "x\n")]
    cache = _LineStyleCache()
    assert cache.start_style(lines, 2) == Style(fg=6)


def test_cell_width_helpers() -> None:
    assert _cells("abc") == 3
    assert _cells("日本語") == 6  # double-width CJK
    assert _clip_cells("abc", 2) == ("ab", 2)
    assert _clip_cells("日本語", 5) == ("日本", 4)  # third char won't fit
    assert _clip_cells("日本語", 6) == ("日本語", 6)
