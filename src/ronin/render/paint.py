"""Layers 2-4 — prepare, layout, compose.

Functions take a Grid and mutate it. Pure CPU. Never touch stdout.

Public surface:
    paint_text(grid, text, x, y, *, style, width=None, wrap=False)
    paint_lines(grid, lines, x, y, *, style)
    paint_centered(grid, lines, *, style, y_offset=0)
    fill_region(grid, x, y, w, h, *, style, char=' ')
"""

from __future__ import annotations

from .cells import Cell, Grid, Style, DEFAULT_STYLE
from .measure import char_width, strip_ansi


def paint_text(
    grid: Grid,
    text: str,
    x: int,
    y: int,
    *,
    style: Style = DEFAULT_STYLE,
    width: int | None = None,
    wrap: bool = False,
) -> int:
    """Paint a logical line of text into the grid starting at (x, y).

    width caps how many cells we'll consume horizontally. If wrap is True
    we drop to the next row when we hit the cap; otherwise we truncate.
    Returns the number of grid rows consumed.
    """
    if y < 0 or y >= grid.rows:
        return 0
    text = strip_ansi(text)
    max_x = grid.cols if width is None else min(grid.cols, x + width)
    cx = x
    cy = y
    rows_used = 1
    for ch in text:
        w = char_width(ch)
        if w == 0:
            # Combining mark — attach to previous cell if there is one.
            if cx - 1 >= 0 and cy < grid.rows and 0 <= cx - 1 < grid.cols:
                prev = grid.at(cy, cx - 1)
                grid.set(cy, cx - 1, Cell(prev.char + ch, prev.style))
            continue
        if cx + w > max_x:
            if not wrap:
                break
            cy += 1
            rows_used += 1
            cx = x
            if cy >= grid.rows:
                break
        if 0 <= cx < grid.cols:
            grid.set(cy, cx, Cell(ch, style))
            if w == 2 and cx + 1 < grid.cols:
                grid.set(cy, cx + 1, Cell("", style, wide_tail=True))
        cx += w
    return rows_used


def paint_lines(
    grid: Grid,
    lines: list[str] | tuple[str, ...],
    x: int,
    y: int,
    *,
    style: Style = DEFAULT_STYLE,
) -> None:
    """Paint a block of pre-formatted lines into the grid.

    Used for ASCII / braille art where lines are already laid out and we
    just need to clip to the grid. No wrapping, no width measurement
    beyond per-character.
    """
    for i, line in enumerate(lines):
        ry = y + i
        if ry < 0 or ry >= grid.rows:
            continue
        cx = x
        for ch in line:
            w = char_width(ch)
            if w == 0:
                continue
            if 0 <= cx < grid.cols:
                grid.set(ry, cx, Cell(ch, style))
                if w == 2 and cx + 1 < grid.cols:
                    grid.set(ry, cx + 1, Cell("", style, wide_tail=True))
            cx += w


def paint_centered(
    grid: Grid,
    lines: list[str] | tuple[str, ...],
    *,
    style: Style = DEFAULT_STYLE,
    y_offset: int = 0,
) -> tuple[int, int]:
    """Paint a block centered on the grid. Returns the (x, y) origin used."""
    if not lines:
        return (0, 0)
    block_h = len(lines)
    block_w = max(
        (sum(char_width(c) for c in line) for line in lines),
        default=0,
    )
    x = max(0, (grid.cols - block_w) // 2)
    y = max(0, (grid.rows - block_h) // 2 + y_offset)
    paint_lines(grid, lines, x, y, style=style)
    return (x, y)


def fill_region(
    grid: Grid,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    style: Style = DEFAULT_STYLE,
    char: str = " ",
) -> None:
    """Fill a rectangular region with a single styled character."""
    cell = Cell(char, style)
    y0 = max(0, y)
    y1 = min(grid.rows, y + h)
    x0 = max(0, x)
    x1 = min(grid.cols, x + w)
    for row in range(y0, y1):
        for col in range(x0, x1):
            grid.set(row, col, cell)
