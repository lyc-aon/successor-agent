"""Viewport decisions for the chat history region."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ViewportDecision:
    body_x: int
    body_width: int
    chat_height: int
    committed_height: int
    scroll_offset: int
    auto_scroll: bool
    last_chat_h: int
    last_chat_w: int
    last_total_height: int
    start: int
    end: int
    paint_y: int


def compute_viewport_decision(
    *,
    width: int,
    top: int,
    bottom: int,
    density: object,
    committed_height: int,
    scroll_offset: int,
    auto_scroll: bool,
    last_total_height: int,
) -> ViewportDecision:
    """Compute body geometry and visible slice for the current chat frame."""
    gutter = density.gutter
    avail = max(1, width - 2 * gutter)
    avail = min(avail, density.max_content_width)
    body_width = avail
    body_x = max(gutter, (width - body_width) // 2)
    chat_height = max(0, bottom - top)

    next_scroll_offset = scroll_offset
    next_auto_scroll = auto_scroll

    if not next_auto_scroll and committed_height > last_total_height:
        next_scroll_offset += committed_height - last_total_height

    max_off = max(0, committed_height - chat_height)
    if next_scroll_offset > max_off:
        next_scroll_offset = max_off
    if next_scroll_offset < 0:
        next_scroll_offset = 0
    if next_scroll_offset == 0:
        next_auto_scroll = True

    end = committed_height - next_scroll_offset
    start = max(0, end - chat_height)
    visible_len = max(0, end - start)
    paint_y = bottom - visible_len
    if paint_y < top:
        paint_y = top

    return ViewportDecision(
        body_x=body_x,
        body_width=body_width,
        chat_height=chat_height,
        committed_height=committed_height,
        scroll_offset=next_scroll_offset,
        auto_scroll=next_auto_scroll,
        last_chat_h=chat_height,
        last_chat_w=body_width,
        last_total_height=committed_height,
        start=start,
        end=end,
        paint_y=paint_y,
    )
