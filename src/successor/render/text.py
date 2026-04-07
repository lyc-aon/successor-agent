"""Text primitives for chat-shaped rendering.

PreparedText is Pretext-shaped: tokenize the source ONCE into wrap-
respecting tokens, then lay out at any width on demand with width-keyed
caching. Resizing the terminal triggers exactly one wrap pass per
visible message; no re-tokenization.

Also exports a few small interpolation helpers used by the chat demo:
hard_wrap (character-level wrapping for input boxes), lerp_rgb (24-bit
color interpolation), and ease_out_cubic.
"""

from __future__ import annotations

from dataclasses import dataclass

from .measure import char_width, strip_ansi


# ─── Tokens ───


@dataclass(slots=True)
class _Token:
    """One word, whitespace run, or hard newline.

    width is the precomputed display width in cells, so the wrap loop
    doesn't have to call char_width again per token.
    """
    text: str
    width: int
    kind: str  # "word" | "space" | "newline"


def _tokenize(source: str) -> list[_Token]:
    """Split source text into wrap-respecting tokens.

    A "word" is a maximal run of non-whitespace, non-newline characters.
    A "space" is a maximal run of horizontal whitespace.
    A "newline" is a single \\n (multiple newlines yield multiple
    newline tokens, which produces multiple line breaks).
    """
    out: list[_Token] = []
    if not source:
        return out
    text = strip_ansi(source)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append(_Token("\n", 0, "newline"))
            i += 1
            continue
        if ch.isspace():
            j = i
            while j < n and text[j] != "\n" and text[j].isspace():
                j += 1
            chunk = text[i:j]
            out.append(_Token(chunk, sum(char_width(c) for c in chunk), "space"))
            i = j
        else:
            j = i
            while j < n and text[j] != "\n" and not text[j].isspace():
                j += 1
            chunk = text[i:j]
            out.append(_Token(chunk, sum(char_width(c) for c in chunk), "word"))
            i = j
    return out


# ─── PreparedText ───


class PreparedText:
    """A piece of text that can be wrapped to any cell width.

    Parse cost is paid ONCE in __init__. Each call to lines(width) is
    pure: it walks the cached tokens and decides where to break lines.
    Results are cached for the most recent width — calling lines(80)
    twice in a row is free; calling lines(80) then lines(120) pays one
    wrap pass for the new width and caches that.

    Pretext analog: __init__ is `prepare()`, lines() is `layout()`.
    """

    __slots__ = ("source", "_tokens", "_cache_w", "_cache_lines")

    def __init__(self, source: str) -> None:
        self.source = source
        self._tokens: list[_Token] = _tokenize(source)
        self._cache_w: int = -1
        self._cache_lines: list[str] = []

    def lines(self, width: int) -> list[str]:
        """Return wrapped lines for the given target width (in cells)."""
        if width <= 0:
            return []
        if width == self._cache_w:
            return self._cache_lines
        self._cache_lines = self._wrap(width)
        self._cache_w = width
        return self._cache_lines

    def height(self, width: int) -> int:
        return len(self.lines(width))

    def _wrap(self, width: int) -> list[str]:
        """Greedy word-wrap to width.

        Whitespace tokens at the start of a wrapped line are dropped.
        Newline tokens force a hard break. Words longer than width get
        broken in the middle (no other option).
        """
        lines: list[str] = []
        current = ""
        current_w = 0

        for tok in self._tokens:
            if tok.kind == "newline":
                lines.append(current)
                current = ""
                current_w = 0
                continue

            if tok.kind == "space":
                if current_w == 0:
                    continue  # don't lead a wrapped line with space
                if current_w + tok.width <= width:
                    current += tok.text
                    current_w += tok.width
                else:
                    lines.append(current)
                    current = ""
                    current_w = 0
                continue

            # word
            if tok.width <= width:
                if current_w + tok.width <= width:
                    current += tok.text
                    current_w += tok.width
                else:
                    lines.append(current)
                    current = tok.text
                    current_w = tok.width
            else:
                # Word longer than the available width — hard-break it.
                if current_w > 0:
                    lines.append(current)
                    current = ""
                    current_w = 0
                chunk = ""
                chunk_w = 0
                for ch in tok.text:
                    cw = char_width(ch)
                    if chunk_w + cw > width:
                        lines.append(chunk)
                        chunk = ch
                        chunk_w = cw
                    else:
                        chunk += ch
                        chunk_w += cw
                if chunk:
                    current = chunk
                    current_w = chunk_w

        if current or not lines:
            lines.append(current)
        return lines


# ─── Plain hard-wrap for input boxes ───


def hard_wrap(text: str, width: int) -> list[str]:
    """Break text into width-sized chunks with no word-boundary respect.

    Use this for input boxes where every character the user typed must
    appear exactly as typed — no whitespace collapsing, no leading-space
    stripping on wrapped lines.

    A leading or trailing newline produces an empty line. Combining
    marks attach to the previous character (don't trigger a wrap).
    """
    if width <= 0:
        return [""]
    if not text:
        return [""]
    out: list[str] = []
    line = ""
    line_w = 0
    for ch in text:
        # Newlines first — char_width('\n') is 0, so we have to check
        # this BEFORE the zero-width branch or every \n would attach
        # to the current line as a literal control char and never
        # produce a row break.
        if ch == "\n":
            out.append(line)
            line = ""
            line_w = 0
            continue
        cw = char_width(ch)
        if cw == 0:
            line += ch
            continue
        if line_w + cw > width:
            out.append(line)
            line = ch
            line_w = cw
        else:
            line += ch
            line_w += cw
    out.append(line)
    return out


# ─── Animation / interpolation helpers ───


def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation, clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def lerp_rgb(a: int, b: int, t: float) -> int:
    """Linearly interpolate between two 24-bit packed RGB colors.

    a and b are 0xRRGGBB integers. t is clamped to [0, 1].
    """
    t = max(0.0, min(1.0, t))
    ar = (a >> 16) & 0xFF
    ag = (a >> 8) & 0xFF
    ab = a & 0xFF
    br = (b >> 16) & 0xFF
    bg = (b >> 8) & 0xFF
    bb = b & 0xFF
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    bo = int(ab + (bb - ab) * t)
    return (r << 16) | (g << 8) | bo


def ease_out_cubic(t: float) -> float:
    """Cubic ease-out: slow start, fast finish, decelerating to 1."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    """Cubic ease-in-out: slow start, fast middle, slow finish."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0
