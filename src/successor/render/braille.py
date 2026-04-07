"""Braille frame loader and dot-level interpolation.

Direct port of a TypeScript braille codec + Bayer-dot interpolator.
The original implementation lives in another personal project; this
file is the standalone Python equivalent.

Unicode Braille Patterns block: U+2800–U+28FF. Each codepoint encodes 8
dots as an 8-bit value where dots 1–6 occupy bits 0–5 (the classic 6-dot
cell) and dots 7–8 extend into bits 6–7 (computer braille).

Bit layout (1-indexed dots, per Unicode spec):

    Dot 1 (bit 0)  Dot 4 (bit 3)
    Dot 2 (bit 1)  Dot 5 (bit 4)
    Dot 3 (bit 2)  Dot 6 (bit 5)
    Dot 7 (bit 6)  Dot 8 (bit 7)

Interpolation: Bayer-like ordered dithering at the dot level. For each
dot that differs between source and target, we assign a flip threshold
in [0, 1]. As progress t increases, dots flip when t crosses their
threshold. The result is a deterministic spatially-uniform morph with
zero RNG.
"""

from __future__ import annotations

from pathlib import Path

# ─── Constants ───

BRAILLE_BASE = 0x2800
BRAILLE_BLANK = "\u2800"
BRAILLE_FULL = "\u28FF"

# Bayer dot order — direct port of BAYER_DOT_ORDER from interpolate.ts.
# Each entry is (dot_bit_mask, threshold) with threshold ∈ [0, 7].
# A dot flips when t >= (threshold + 0.5) / 8.
BAYER_DOT_ORDER: tuple[tuple[int, int], ...] = (
    (0x01, 0),  # Dot 1 — top-left
    (0x04, 1),  # Dot 3 — mid-lower-left
    (0x10, 2),  # Dot 5 — mid-upper-right
    (0x80, 3),  # Dot 8 — bottom-right
    (0x08, 4),  # Dot 4 — top-right
    (0x20, 5),  # Dot 6 — mid-lower-right
    (0x02, 6),  # Dot 2 — mid-upper-left
    (0x40, 7),  # Dot 7 — bottom-left
)

# Pre-computed flip thresholds — pulled out of the inner loop.
_BAYER_FLIP_AT: tuple[tuple[int, float], ...] = tuple(
    (bit, (thresh + 0.5) / 8.0) for bit, thresh in BAYER_DOT_ORDER
)


# ─── Codec ───


def braille_to_bits(ch: str) -> int:
    """Extract the 8-bit dot pattern from a braille character.

    Returns 0 for non-braille input (treats it as a blank cell).
    """
    if not ch:
        return 0
    cp = ord(ch[0])
    if cp < BRAILLE_BASE or cp > BRAILLE_BASE + 0xFF:
        return 0
    return cp - BRAILLE_BASE


def bits_to_braille(bits: int) -> str:
    """Convert an 8-bit dot pattern to its braille character."""
    return chr(BRAILLE_BASE + (bits & 0xFF))


# ─── Interpolation ───


def interpolate_cell(bits_a: int, bits_b: int, t: float) -> int:
    """Interpolate a single braille cell using Bayer ordered dithering.

    Direct port of `interpolateCell` from interpolate.ts.

    bits_a: source 8-bit pattern
    bits_b: target 8-bit pattern
    t:      progress in [0, 1] (already eased if applicable)
    """
    if t <= 0.0:
        return bits_a
    if t >= 1.0:
        return bits_b
    if bits_a == bits_b:
        return bits_a
    diff = bits_a ^ bits_b
    result = bits_a
    for dot_bit, flip_at in _BAYER_FLIP_AT:
        if not (diff & dot_bit):
            continue
        if t >= flip_at:
            if bits_b & dot_bit:
                result |= dot_bit
            else:
                result &= ~dot_bit
    return result


def interpolate_frame(
    frame_a: list[str],
    frame_b: list[str],
    t: float,
) -> list[str]:
    """Interpolate two braille frames line-by-line, character-by-character."""
    if t <= 0.0:
        return list(frame_a)
    if t >= 1.0:
        return list(frame_b)
    out: list[str] = []
    rows = max(len(frame_a), len(frame_b))
    for r in range(rows):
        line_a = frame_a[r] if r < len(frame_a) else ""
        line_b = frame_b[r] if r < len(frame_b) else ""
        cols = max(len(line_a), len(line_b))
        chars: list[str] = []
        for c in range(cols):
            ca = line_a[c] if c < len(line_a) else BRAILLE_BLANK
            cb = line_b[c] if c < len(line_b) else BRAILLE_BLANK
            ba = braille_to_bits(ca)
            bb = braille_to_bits(cb)
            chars.append(bits_to_braille(interpolate_cell(ba, bb, t)))
        out.append("".join(chars))
    return out


# ─── I/O ───


def load_frame(path: str | Path) -> list[str]:
    """Load a braille art file. Strips trailing newlines and CRs.

    Empty trailing lines are dropped to avoid wasted vertical space.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    lines = [line.rstrip("\r\n") for line in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


# ─── Pretext-shaped resampling layer ───
#
# Everything below this line is the prepare/layout split applied to braille
# art. Inspired by https://www.pretext.cool/ — Cheng Lou's library that
# separates expensive measurement from cheap re-layout, in pure userland.
#
# In a browser the slow side is `getBoundingClientRect`. In a terminal
# the slow side is parsing braille codepoints into their constituent
# dot bitmaps and resampling those bitmaps to a target resolution.
# We do that work ONCE per frame (parse) and cache the resample result
# per target size, so a terminal resize triggers exactly one resample
# pass per visible frame and zero re-parse work.

# Per-cell dot position → bit mask. DOT_AT[sub_row][sub_col] = bit.
# Source: codec.ts DOT_GRID + DOT_BITS combined into a 4×2 lookup.
DOT_AT: tuple[tuple[int, int], ...] = (
    (0x01, 0x08),  # row 0: dot 1, dot 4
    (0x02, 0x10),  # row 1: dot 2, dot 5
    (0x04, 0x20),  # row 2: dot 3, dot 6
    (0x40, 0x80),  # row 3: dot 7, dot 8
)


def parse_dots(source: list[str]) -> list[list[bool]]:
    """Parse a braille frame into a 2D dot bitmap.

    Output dimensions: (rows*4, cols*2). Each braille cell expands into
    a 4-row × 2-col block of bools, exactly as the Unicode spec lays them
    out. Non-braille characters are treated as blanks.

    This is the "prepare" half of the prepare/layout split: it walks the
    source string once and produces an immutable bitmap that can be
    re-laid-out at any target size without re-parsing.
    """
    src_rows = len(source)
    src_cols = max((len(line) for line in source), default=0)
    rows = src_rows * 4
    cols = src_cols * 2
    out = [[False] * cols for _ in range(rows)]
    for cy, line in enumerate(source):
        base_y = cy * 4
        for cx, ch in enumerate(line):
            bits = braille_to_bits(ch)
            if bits == 0:
                continue
            base_x = cx * 2
            for sr in range(4):
                row = out[base_y + sr]
                bit_left = DOT_AT[sr][0]
                bit_right = DOT_AT[sr][1]
                if bits & bit_left:
                    row[base_x] = True
                if bits & bit_right:
                    row[base_x + 1] = True
    return out


def pack_dots(dots: list[list[bool]]) -> list[str]:
    """Pack a 2D dot bitmap back into braille characters.

    The bitmap dimensions don't need to be aligned to (4, 2); any partial
    cell at the right or bottom edge is treated as having empty dots
    in the unfilled positions.
    """
    if not dots or not dots[0]:
        return []
    h = len(dots)
    w = len(dots[0])
    cell_rows = (h + 3) // 4
    cell_cols = (w + 1) // 2
    out: list[str] = []
    for cy in range(cell_rows):
        chars: list[str] = []
        for cx in range(cell_cols):
            bits = 0
            for sr in range(4):
                py = cy * 4 + sr
                if py >= h:
                    continue
                row = dots[py]
                px_l = cx * 2
                px_r = px_l + 1
                if px_l < w and row[px_l]:
                    bits |= DOT_AT[sr][0]
                if px_r < w and row[px_r]:
                    bits |= DOT_AT[sr][1]
            chars.append(bits_to_braille(bits))
        out.append("".join(chars))
    return out


def resample_dots(
    src: list[list[bool]],
    dst_h: int,
    dst_w: int,
    *,
    threshold: float = 0.40,
) -> list[list[bool]]:
    """Area-average resample of a binary dot bitmap.

    For each destination dot, computes the source rectangle it covers,
    sums the lit area with fractional pixel weights, and sets the dst
    dot ON if the lit fraction exceeds threshold.

    Works for both upscaling (each dst dot maps to a sub-pixel of one
    src dot — effectively nearest-neighbor) and downscaling (each dst
    dot averages multiple src dots).

    threshold:
      0.0  every dst dot ON if any source area is lit (very fat)
      0.4  good default for braille art — preserves silhouettes
      0.5  needs majority lit area (skinnier figures)
      1.0  every dst dot OFF unless 100% covered (very thin)
    """
    src_h = len(src)
    src_w = len(src[0]) if src else 0
    if src_h == 0 or src_w == 0 or dst_h <= 0 or dst_w <= 0:
        return [[False] * max(0, dst_w) for _ in range(max(0, dst_h))]
    if src_h == dst_h and src_w == dst_w:
        return [row[:] for row in src]

    sy_step = src_h / dst_h
    sx_step = src_w / dst_w
    out: list[list[bool]] = [[False] * dst_w for _ in range(dst_h)]

    for dy in range(dst_h):
        sy0 = dy * sy_step
        sy1 = sy0 + sy_step
        iy0 = int(sy0)
        iy1 = min(src_h, int(sy1) + 1)
        out_row = out[dy]
        for dx in range(dst_w):
            sx0 = dx * sx_step
            sx1 = sx0 + sx_step
            ix0 = int(sx0)
            ix1 = min(src_w, int(sx1) + 1)
            covered = 0.0
            lit = 0.0
            for iy in range(iy0, iy1):
                wy = min(sy1, iy + 1) - max(sy0, float(iy))
                if wy <= 0:
                    continue
                row = src[iy]
                for ix in range(ix0, ix1):
                    wx = min(sx1, ix + 1) - max(sx0, float(ix))
                    if wx <= 0:
                        continue
                    area = wy * wx
                    covered += area
                    if row[ix]:
                        lit += area
            if covered > 0 and (lit / covered) >= threshold:
                out_row[dx] = True
    return out


class BrailleArt:
    """A braille frame that can be re-laid-out at any cell size.

    Pretext-shaped: the expensive parse step (source string → dot
    bitmap) runs once, in __init__. The fast layout step (dot bitmap →
    target-size dot bitmap → packed braille) is pure and cached for the
    most recent target size. Resizing the terminal triggers exactly one
    resample pass per visible art and zero re-parse work.

    Use:
        art = BrailleArt(load_frame(path))
        lines = art.layout(cells_w=80, cells_h=30)   # cached after 1st call
        more_lines = art.layout(cells_w=120, cells_h=40)  # new size, recompute
    """

    __slots__ = (
        "source",
        "dots",
        "dot_h",
        "dot_w",
        "_layout_key",
        "_layout_value",
    )

    def __init__(self, source: list[str]) -> None:
        self.source = source
        # Prepare: parse braille codepoints into a dot bitmap. Done ONCE.
        self.dots: list[list[bool]] = parse_dots(source)
        self.dot_h: int = len(self.dots)
        self.dot_w: int = len(self.dots[0]) if self.dots else 0
        # Single-entry layout cache. Bounded so a thousand resizes
        # never grow memory; revisiting an old size pays one resample.
        self._layout_key: tuple[int, int] | None = None
        self._layout_value: list[list[bool]] | None = None

    @property
    def src_cells_h(self) -> int:
        return self.dot_h // 4

    @property
    def src_cells_w(self) -> int:
        return self.dot_w // 2

    def layout_dots(self, cells_w: int, cells_h: int) -> list[list[bool]]:
        """Return the resampled dot bitmap for the given cell dimensions."""
        target_dot_w = max(0, cells_w) * 2
        target_dot_h = max(0, cells_h) * 4
        key = (target_dot_h, target_dot_w)
        if self._layout_key == key and self._layout_value is not None:
            return self._layout_value
        result = resample_dots(self.dots, target_dot_h, target_dot_w)
        self._layout_key = key
        self._layout_value = result
        return result

    def layout(self, cells_w: int, cells_h: int) -> list[str]:
        """Return the art as packed braille lines at the target cell size.

        Result is exactly cells_h lines tall and cells_w characters wide
        (assuming cells_w >= 1 and cells_h >= 1). The dot bitmap step
        is cached; only the cheap pack runs after a cache hit.
        """
        return pack_dots(self.layout_dots(cells_w, cells_h))


def fit_dimensions(
    src_dot_h: int,
    src_dot_w: int,
    avail_cells_h: int,
    avail_cells_w: int,
    *,
    pad_cells: int = 1,
) -> tuple[int, int]:
    """Compute the largest cell-aligned (cells_w, cells_h) that preserves
    the source aspect ratio and fits within the available cells.

    avail_cells_h / avail_cells_w are the bounds. pad_cells is subtracted
    from each side. Returns (0, 0) if there's not enough room.

    Aspect is computed in DOT space, not cell space — because braille
    dots are roughly square in a typical 1:2 monospace font (one cell is
    2 dots wide × 4 dots tall, and one cell is 1 unit wide × 2 units
    tall in pixels — so each dot is 0.5 × 0.5, square).
    """
    if src_dot_h <= 0 or src_dot_w <= 0:
        return (0, 0)
    avail_cells_h = avail_cells_h - 2 * pad_cells
    avail_cells_w = avail_cells_w - 2 * pad_cells
    if avail_cells_h <= 0 or avail_cells_w <= 0:
        return (0, 0)
    avail_dot_h = avail_cells_h * 4
    avail_dot_w = avail_cells_w * 2
    src_aspect = src_dot_w / src_dot_h        # width / height
    avail_aspect = avail_dot_w / avail_dot_h
    if avail_aspect >= src_aspect:
        # Available area is wider than the source aspect — height-bound.
        target_dot_h = avail_dot_h
        target_dot_w = round(target_dot_h * src_aspect)
    else:
        # Available area is taller than the source aspect — width-bound.
        target_dot_w = avail_dot_w
        target_dot_h = round(target_dot_w / src_aspect)
    target_cells_h = max(1, target_dot_h // 4)
    target_cells_w = max(1, target_dot_w // 2)
    return (target_cells_w, target_cells_h)
