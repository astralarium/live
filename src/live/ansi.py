"""ANSI escape handling: stripping and SGR parsing into styled spans.

Pure (no curses) so span splitting and color math are unit-testable; the
pager maps `Style` to curses attributes.

`strip_ansi` and `parse_spans` compile from the same pattern, so they always
agree on what counts as an escape — the pager's search-highlight columns
depend on that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# ECMA-48 / VT100 escape sequences, with the CSI params/final captured so
# SGR sequences can be interpreted.
_ANSI_PATTERN = r"""
    \x1B
    (?:
        \[ (?P<csi> [0-?]* ) [ -/]* (?P<final> [@-~] )   # CSI
      | \] [^\x07]*? (?:\x07|\x1B\\)                     # OSC ... BEL or ESC \\
      | [@-_]                                            # 2-byte (Fp, Fe, Fs)
    )
"""
_ANSI_RE = re.compile(_ANSI_PATTERN, re.VERBOSE)
_ANSI_BYTES_RE = re.compile(_ANSI_PATTERN.encode("ascii"), re.VERBOSE)


def strip_ansi(data: bytes) -> bytes:
    """Remove ANSI/VT escape sequences from a byte stream."""
    return _ANSI_BYTES_RE.sub(b"", data)


def strip_ansi_str(text: str) -> str:
    """Remove ANSI/VT escape sequences from a string.

    Uses the same pattern as `parse_spans`, so the result equals its
    concatenated chunks.
    """
    return _ANSI_RE.sub("", text)


@dataclass(frozen=True)
class Style:
    """Resolved SGR display state for a span of text.

    `fg`/`bg` are xterm-256 palette indices, or -1 for the terminal default.
    """

    fg: int = -1
    bg: int = -1
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    reverse: bool = False


DEFAULT_STYLE = Style()


def parse_spans(
    text: str, start: Style = DEFAULT_STYLE
) -> tuple[list[tuple[str, Style]], Style]:
    """Split `text` into `(chunk, style)` spans, dropping escape sequences.

    `start` is the style in effect at the beginning of `text` (carry-over
    from the previous line). Returns the spans plus the style in effect at
    the end, so callers can thread state across lines. The concatenated
    chunks equal `text` with all escapes removed.
    """
    spans: list[tuple[str, Style]] = []
    style = start
    pos = 0
    for m in _ANSI_RE.finditer(text):
        if m.start() > pos:
            spans.append((text[pos : m.start()], style))
        if m.group("final") == "m":
            style = _apply_sgr(style, m.group("csi"))
        pos = m.end()
    if pos < len(text):
        spans.append((text[pos:], style))
    return spans, style


def _apply_sgr(style: Style, raw: str) -> Style:
    """Apply one SGR parameter string (the `...` of `ESC[...m`) to `style`.

    Unknown or malformed parameters are skipped. Both `;`-separated and
    `:`-subparameter (ITU T.416) forms of 38/48 extended colors are accepted.
    """
    tokens = raw.split(";") if raw else [""]
    i = 0
    while i < len(tokens):
        parts = tokens[i].split(":")
        try:
            p = int(parts[0]) if parts[0] else 0
        except ValueError:
            i += 1
            continue
        if p == 0:
            style = DEFAULT_STYLE
        elif p == 1:
            style = replace(style, bold=True)
        elif p == 2:
            style = replace(style, dim=True)
        elif p == 3:
            style = replace(style, italic=True)
        elif p == 4:
            # ITU T.416 colon form: 4:0 clears, 4:1..5 are underline styles.
            style = replace(
                style, underline=len(parts) < 2 or _to_int(parts[1]) != 0
            )
        elif p in (5, 6):
            style = replace(style, blink=True)
        elif p == 7:
            style = replace(style, reverse=True)
        elif p == 22:
            style = replace(style, bold=False, dim=False)
        elif p == 23:
            style = replace(style, italic=False)
        elif p == 24:
            style = replace(style, underline=False)
        elif p == 25:
            style = replace(style, blink=False)
        elif p == 27:
            style = replace(style, reverse=False)
        elif 30 <= p <= 37:
            style = replace(style, fg=p - 30)
        elif p == 39:
            style = replace(style, fg=-1)
        elif 40 <= p <= 47:
            style = replace(style, bg=p - 40)
        elif p == 49:
            style = replace(style, bg=-1)
        elif 90 <= p <= 97:
            style = replace(style, fg=p - 90 + 8)
        elif 100 <= p <= 107:
            style = replace(style, bg=p - 100 + 8)
        elif p in (38, 48):
            color, i = _extended_color(parts, tokens, i)
            if color is not None:
                style = replace(style, fg=color) if p == 38 else replace(style, bg=color)
            continue
        i += 1
    return style


def _to_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


def _extended_color(
    parts: list[str], tokens: list[str], i: int
) -> tuple[int | None, int]:
    """Parse a 38/48 extended color at token `i`.

    Handles `38;5;n`, `38;2;r;g;b`, and the colon forms `38:5:n`,
    `38:2:r:g:b`, `38:2::r:g:b`. Returns the palette index (None if
    malformed) and the index of the next unconsumed token.
    """
    if len(parts) >= 2:  # colon form: subparams live inside this one token
        mode = parts[1]
        if mode == "5" and len(parts) >= 3:
            n = _to_int(parts[2])
            return (n if n is not None and 0 <= n <= 255 else None), i + 1
        if mode == "2":
            raw = parts[3:6] if len(parts) >= 6 else parts[2:5]
            vals = [_to_int(v) for v in raw]
            if len(vals) == 3 and all(
                v is not None and 0 <= v <= 255 for v in vals
            ):
                return rgb_to_256(*vals), i + 1
        return None, i + 1
    mode = tokens[i + 1] if i + 1 < len(tokens) else ""
    if mode == "5":
        n = _to_int(tokens[i + 2]) if i + 2 < len(tokens) else None
        return (n if n is not None and 0 <= n <= 255 else None), i + 3
    if mode == "2":
        vals = [_to_int(v) for v in tokens[i + 2 : i + 5]]
        if len(vals) == 3 and all(v is not None and 0 <= v <= 255 for v in vals):
            return rgb_to_256(*vals), i + 5
        return None, i + 5
    # Unknown mode: consume only the 38/48 so later parameters still apply.
    return None, i + 1


def rgb_to_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 index for a truecolor value.

    Near-gray values map onto the grayscale ramp (232-255), everything else
    onto the 6x6x6 color cube (16-231).
    """
    if max(r, g, b) - min(r, g, b) < 12:
        gray = (r + g + b) // 3
        if gray < 5:
            return 16
        if gray > 246:
            return 231
        # Ramp steps are 8 + 10k; round to the nearest step.
        return 232 + min(23, max(0, (gray - 3) // 10))

    def level(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else min(5, (v - 35) // 40)

    return 16 + 36 * level(r) + 6 * level(g) + level(b)


_BASE16_RGB = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


def palette_rgb(idx: int) -> tuple[int, int, int]:
    """RGB value of an xterm-256 palette index."""
    if idx < 16:
        return _BASE16_RGB[idx]
    if idx < 232:
        idx -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[idx // 36], steps[idx // 6 % 6], steps[idx % 6])
    v = 8 + (idx - 232) * 10
    return (v, v, v)


def to_base16(idx: int) -> int:
    """Nearest of the 16 basic colors, for terminals without 256-color support."""
    r, g, b = palette_rgb(idx)
    best = 0
    best_d: int | None = None
    for i, (cr, cg, cb) in enumerate(_BASE16_RGB):
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best
