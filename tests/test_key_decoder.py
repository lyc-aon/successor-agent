"""Tests for the byte-stream key decoder."""

from __future__ import annotations

from successor.input.keys import Key, KeyDecoder


def _joined_chars(events: list[object]) -> str:
    parts: list[str] = []
    for event in events:
        char = getattr(event, "char", None)
        if char is not None:
            parts.append(char)
    return "".join(parts)


def test_feed_bytes_decodes_mixed_ascii_and_utf8() -> None:
    decoder = KeyDecoder()
    events = decoder.feed_bytes("café".encode("utf-8"))
    assert _joined_chars(events) == "café"


def test_feed_reassembles_utf8_across_separate_calls() -> None:
    decoder = KeyDecoder()
    out: list[object] = []
    for byte in "你好🦊".encode("utf-8"):
        out.extend(decoder.feed(byte))
    assert _joined_chars(out) == "你好🦊"


def test_invalid_utf8_drops_partial_sequence_and_recovers() -> None:
    decoder = KeyDecoder()
    events = decoder.feed_bytes(b"\xc3x\xc3\xa9")
    assert _joined_chars(events) == "xé"


def test_bracketed_paste_coalesces_unicode_chunk() -> None:
    decoder = KeyDecoder()
    events = decoder.feed_bytes(b"\x1b[200~h\xc3\xa9llo \xf0\x9f\xa6\x8a\x1b[201~")
    assert len(events) == 3
    assert events[0].key == Key.PASTE_START
    assert events[1].char == "héllo 🦊"
    assert events[2].key == Key.PASTE_END
