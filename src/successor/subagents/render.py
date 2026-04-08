"""Renderer for subagent spawn cards."""

from __future__ import annotations

from ..render.cells import ATTR_BOLD, ATTR_DIM, ATTR_ITALIC, Grid, Style
from ..render.paint import BOX_ROUND, fill_region, paint_box, paint_text
from ..render.text import hard_wrap
from ..render.theme import ThemeVariant
from .cards import SubagentToolCard

_INNER_PAD_X = 1
_OUTPUT_INDENT = 2
_MAX_OUTPUT_LINES = 4


def _directive_label(text: str, width: int) -> str:
    first, _, rest = text.partition("\n")
    label = first
    if rest:
        label = f"{first}  (+{text.count(chr(10))} lines)"
    if len(label) <= width:
        return label
    return label[: max(0, width - 1)] + "…"


def _wrapped_output(card: SubagentToolCard, width: int) -> list[str]:
    avail = max(10, width - _OUTPUT_INDENT - 2)
    lines = hard_wrap(card.spawn_result, avail)
    if len(lines) > _MAX_OUTPUT_LINES:
        head = lines[: _MAX_OUTPUT_LINES - 1]
        head.append("…")
        return head
    return lines


def measure_subagent_card_height(card: SubagentToolCard, *, width: int) -> int:
    if width < 24:
        return 0
    params = 3 if card.name else 2
    return 2 + params + len(_wrapped_output(card, width))


def paint_subagent_card(
    grid: Grid,
    card: SubagentToolCard,
    *,
    x: int,
    y: int,
    w: int,
    theme: ThemeVariant,
) -> int:
    """Paint a spawned-subagent card into the grid."""
    if w < 24 or y >= grid.rows:
        return 0

    params: list[tuple[str, str]] = [
        ("task", card.task_id),
        ("status", "queued"),
    ]
    if card.name:
        params.insert(1, ("name", card.name))
    box_h = 2 + len(params)
    border_style = Style(fg=theme.accent_warm, bg=theme.bg, attrs=ATTR_BOLD)
    inner_style = Style(fg=theme.fg, bg=theme.bg_input)
    paint_box(
        grid, x, y, w, box_h,
        style=border_style,
        fill_style=inner_style,
        chars=BOX_ROUND,
    )

    header = " ⎇ subagent "
    header = header[: max(0, w - 4)]
    paint_text(
        grid,
        header,
        x + 3,
        y,
        style=Style(fg=theme.bg, bg=theme.accent_warm, attrs=ATTR_BOLD),
    )

    label_w = max(len(key) for key, _ in params)
    body_x = x + _INNER_PAD_X + 1
    for idx, (key, value) in enumerate(params):
        row_y = y + 1 + idx
        paint_text(
            grid,
            key.rjust(label_w),
            body_x,
            row_y,
            style=Style(fg=theme.fg_dim, bg=theme.bg_input, attrs=ATTR_DIM),
        )
        paint_text(
            grid,
            value,
            body_x + label_w + 2,
            row_y,
            style=Style(fg=theme.fg, bg=theme.bg_input, attrs=ATTR_BOLD),
        )

    prompt_label = " » " + _directive_label(card.directive, max(10, w - 8)) + " "
    paint_text(
        grid,
        prompt_label[: max(0, w - 4)],
        x + 3,
        y + box_h - 1,
        style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
    )

    cur_y = y + box_h
    for line in _wrapped_output(card, w):
        if cur_y >= grid.rows:
            break
        fill_region(grid, x + 1, cur_y, w - 2, 1, style=Style(bg=theme.bg_input))
        paint_text(
            grid,
            line,
            x + _OUTPUT_INDENT,
            cur_y,
            style=Style(fg=theme.fg_subtle, bg=theme.bg_input, attrs=ATTR_DIM),
        )
        cur_y += 1

    return cur_y - y
