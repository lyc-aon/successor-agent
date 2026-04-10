"""Chat-scene layout primitives shared by the render helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .cells import Style


@dataclass(slots=True, frozen=True)
class HitBox:
    x: int
    y: int
    w: int
    h: int
    action: str

    def contains(self, col: int, row: int) -> bool:
        return (
            self.x <= col < self.x + self.w
            and self.y <= row < self.y + self.h
        )


@dataclass(slots=True, frozen=True)
class PlacedText:
    text: str
    x: int
    y: int
    style: Style
    action: str | None = None


@dataclass(slots=True, frozen=True)
class HeaderPlan:
    placements: tuple[PlacedText, ...]
    hitboxes: tuple[HitBox, ...]


@dataclass(slots=True, frozen=True)
class ChatFrameLayout:
    rows: int
    cols: int
    title_h: int
    input_h: int
    footer_h: int
    static_y: int
    input_y: int
    chat_top: int
    chat_bottom: int


def compute_chat_frame(
    rows: int,
    cols: int,
    input_h: int,
    *,
    title_h: int = 1,
    footer_h: int = 1,
) -> ChatFrameLayout:
    """Compute the fixed regions for one chat frame."""
    static_y = rows - footer_h
    input_y = static_y - input_h
    chat_top = title_h
    chat_bottom = max(chat_top, input_y)
    return ChatFrameLayout(
        rows=rows,
        cols=cols,
        title_h=title_h,
        input_h=input_h,
        footer_h=footer_h,
        static_y=static_y,
        input_y=input_y,
        chat_top=chat_top,
        chat_bottom=chat_bottom,
    )
