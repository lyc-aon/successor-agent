"""Layer 1 — grapheme width measurement.

Pure functions. No I/O. The "measure" half of Pretext's prepare/layout
split: expensive grapheme inspection happens here, once, and the result
feeds into fast pure-arithmetic layout.

For the v0 renderer we support:
  - ASCII (width 1)
  - Braille block U+2800-U+28FF (width 1)
  - General Latin / common BMP (width 1)
  - East Asian Wide (width 2)
  - Combining marks / format / zero-width (width 0)
  - ANSI CSI escape sequences (width 0)

We deliberately do NOT depend on `wcwidth` or `grapheme` packages — Successor's
renderer must work in a pure-stdlib install. The wide-range table below is
the conservative subset of Unicode 15 East Asian Wide / Fullwidth ranges
that covers everything Successor will plausibly render in v0.

Reference:
  https://www.unicode.org/reports/tr11/   (East Asian Width)
  https://www.unicode.org/charts/PDF/U2800.pdf  (Braille Patterns)
"""

from __future__ import annotations

import re
import unicodedata

# CSI ANSI escape sequences contribute zero columns to display width.
# Pattern: ESC [ <params> <intermediates> <final-byte>
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Conservative East Asian Wide / Fullwidth ranges (Unicode 15.x).
# Sorted ascending; binary-searchable but linear is fine for v0.
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),    # Hangul Jamo
    (0x231A, 0x231B),
    (0x2329, 0x232A),
    (0x23E9, 0x23EC),
    (0x23F0, 0x23F0),
    (0x23F3, 0x23F3),
    (0x25FD, 0x25FE),
    (0x2614, 0x2615),
    (0x2648, 0x2653),
    (0x267F, 0x267F),
    (0x2693, 0x2693),
    (0x26A1, 0x26A1),
    (0x26AA, 0x26AB),
    (0x26BD, 0x26BE),
    (0x26C4, 0x26C5),
    (0x26CE, 0x26CE),
    (0x26D4, 0x26D4),
    (0x26EA, 0x26EA),
    (0x26F2, 0x26F3),
    (0x26F5, 0x26F5),
    (0x26FA, 0x26FA),
    (0x26FD, 0x26FD),
    (0x2705, 0x2705),
    (0x270A, 0x270B),
    (0x2728, 0x2728),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2795, 0x2797),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x2E80, 0x303E),    # CJK Radicals / Symbols
    (0x3041, 0x33FF),    # Hiragana / Katakana / CJK Symbols
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xA000, 0xA4CF),    # Yi
    (0xAC00, 0xD7A3),    # Hangul Syllables
    (0xF900, 0xFAFF),    # CJK Compat
    (0xFE30, 0xFE4F),    # CJK Compat Forms
    (0xFF00, 0xFF60),    # Fullwidth Forms
    (0xFFE0, 0xFFE6),
    (0x1F300, 0x1F64F),  # Misc Symbols / Emoticons
    (0x1F680, 0x1F6FF),  # Transport
    (0x1F900, 0x1F9FF),  # Supplemental Symbols
    (0x20000, 0x2FFFD),  # CJK Extensions B-F
    (0x30000, 0x3FFFD),
)


def _is_wide(cp: int) -> bool:
    for lo, hi in _WIDE_RANGES:
        if cp < lo:
            return False
        if cp <= hi:
            return True
    return False


def char_width(ch: str) -> int:
    """Display width of a single Unicode character.

    Returns 0 for combining marks, format chars, and C0/C1 controls.
    Returns 2 for East Asian Wide / Fullwidth / wide emoji.
    Returns 1 for everything else.
    """
    if not ch:
        return 0
    cp = ord(ch[0])
    if cp == 0:
        return 0
    if cp < 0x20 or cp == 0x7F:
        return 0
    cat = unicodedata.category(ch[0])
    # Mn = nonspacing mark, Me = enclosing mark, Cf = format (ZWJ, ZWNJ, BOM…)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    if _is_wide(cp):
        return 2
    return 1


def text_width(s: str) -> int:
    """Display width of a string with ANSI escapes stripped."""
    if not s:
        return 0
    s = _ANSI_RE.sub("", s)
    return sum(char_width(ch) for ch in s)


def strip_ansi(s: str) -> str:
    """Remove CSI escape sequences from a string."""
    return _ANSI_RE.sub("", s)
