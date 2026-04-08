"""Unicode grapheme helpers for text editing.

Successor keeps runtime deps at zero, so these helpers implement the
small slice of grapheme-cluster logic the editors need using only the
stdlib. The goal is pragmatic: backspace/delete should act on what the
user sees as one character rather than slicing raw codepoints.
"""

from __future__ import annotations

import unicodedata


_ZWJ = "\u200d"


def _is_variation_selector(ch: str) -> bool:
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def _is_emoji_modifier(ch: str) -> bool:
    cp = ord(ch)
    return 0x1F3FB <= cp <= 0x1F3FF


def _is_regional_indicator(ch: str) -> bool:
    cp = ord(ch)
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_extend(ch: str) -> bool:
    return (
        unicodedata.category(ch) in ("Mn", "Mc", "Me")
        or _is_variation_selector(ch)
        or _is_emoji_modifier(ch)
    )


def prev_grapheme_boundary(text: str, index: int) -> int:
    """Start offset of the grapheme cluster immediately before `index`."""
    index = max(0, min(index, len(text)))
    if index == 0:
        return 0

    if _is_regional_indicator(text[index - 1]):
        run_start = index - 1
        while run_start > 0 and _is_regional_indicator(text[run_start - 1]):
            run_start -= 1
        run_len = index - run_start
        return index - 2 if run_len % 2 == 0 else index - 1

    start = index - 1
    while start > 0 and _is_extend(text[start]):
        start -= 1

    while start > 0 and text[start - 1] == _ZWJ:
        start -= 1  # include the joiner
        if start == 0:
            break
        start -= 1  # include the previous base
        while start > 0 and _is_extend(text[start]):
            start -= 1

    return start


def next_grapheme_boundary(text: str, index: int) -> int:
    """End offset of the grapheme cluster that starts at `index`."""
    index = max(0, min(index, len(text)))
    if index >= len(text):
        return len(text)

    if _is_regional_indicator(text[index]):
        run_start = index
        while run_start > 0 and _is_regional_indicator(text[run_start - 1]):
            run_start -= 1
        run_end = index + 1
        while run_end < len(text) and _is_regional_indicator(text[run_end]):
            run_end += 1
        offset = index - run_start
        if offset % 2 == 0 and index + 1 < run_end:
            return index + 2
        return index + 1

    end = index + 1
    while end < len(text) and _is_extend(text[end]):
        end += 1

    while end < len(text) and text[end] == _ZWJ:
        end += 1
        if end >= len(text):
            break
        end += 1
        while end < len(text) and _is_extend(text[end]):
            end += 1

    return end


def delete_prev_grapheme(text: str, cursor: int) -> tuple[str, int]:
    """Delete the grapheme cluster immediately before `cursor`."""
    cursor = max(0, min(cursor, len(text)))
    start = prev_grapheme_boundary(text, cursor)
    return (text[:start] + text[cursor:], start)


def delete_next_grapheme(text: str, cursor: int) -> tuple[str, int]:
    """Delete the grapheme cluster that starts at `cursor`."""
    cursor = max(0, min(cursor, len(text)))
    end = next_grapheme_boundary(text, cursor)
    return (text[:cursor] + text[end:], cursor)
