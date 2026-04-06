"""Braille frame loader and dot-level interpolation.

Direct port of:
  /home/lycaon/dev/web/lycaonwtf/src/lib/braille/codec.ts
  /home/lycaon/dev/web/lycaonwtf/src/lib/braille/interpolate.ts

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
