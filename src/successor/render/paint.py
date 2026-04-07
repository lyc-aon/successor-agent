"""Layers 2-4 — prepare, layout, compose.

Functions take a Grid and mutate it. Pure CPU. Never touch stdout.

Public surface:
    paint_text(grid, text, x, y, *, style, width=None, wrap=False)
    paint_lines(grid, lines, x, y, *, style)
    paint_centered(grid, lines, *, style, y_offset=0)
    fill_region(grid, x, y, w, h, *, style, char=' ')
    paint_box(grid, x, y, w, h, *, style, fill_style, fill_char, chars)
    paint_horizontal_divider(grid, x, y, w, *, style, char, t)
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


# Box-drawing constants for paint_box. Using rounded corners by default
# because they look softer in mono fonts. Square is also available.
BOX_ROUND = ("\u256d", "\u256e", "\u2570", "\u256f", "\u2500", "\u2502")
BOX_SQUARE = ("\u250c", "\u2510", "\u2514", "\u2518", "\u2500", "\u2502")
BOX_HEAVY = ("\u250f", "\u2513", "\u2517", "\u251b", "\u2501", "\u2503")
#                                                       (top-l, top-r,
#                                                        bot-l, bot-r,
#                                                        horiz, vert)


def paint_box(
    grid: Grid,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    style: Style = DEFAULT_STYLE,
    fill_style: Style | None = None,
    fill_char: str = " ",
    chars: tuple[str, str, str, str, str, str] = BOX_ROUND,
) -> None:
    """Draw a thin-bordered rectangle with line-drawing chars.

    The interior is filled with `fill_char` styled by `fill_style`
    (defaults to the same style as the border, which gives a clean
    monochrome popover look). Use a different fill_style to get a
    border-on-tinted-bg appearance.

    Out-of-bounds coordinates are clipped silently. The minimum
    drawable size is 2x2.
    """
    if w < 2 or h < 2:
        return
    fill_style = fill_style if fill_style is not None else style
    tl, tr, bl, br, hbar, vbar = chars

    # Interior fill (inset 1 cell on every side)
    fill_region(
        grid, x + 1, y + 1, w - 2, h - 2,
        style=fill_style, char=fill_char,
    )

    # Top border
    if 0 <= y < grid.rows:
        if 0 <= x < grid.cols:
            grid.set(y, x, Cell(tl, style))
        for cx in range(x + 1, x + w - 1):
            if 0 <= cx < grid.cols:
                grid.set(y, cx, Cell(hbar, style))
        if 0 <= x + w - 1 < grid.cols:
            grid.set(y, x + w - 1, Cell(tr, style))

    # Bottom border
    by = y + h - 1
    if 0 <= by < grid.rows:
        if 0 <= x < grid.cols:
            grid.set(by, x, Cell(bl, style))
        for cx in range(x + 1, x + w - 1):
            if 0 <= cx < grid.cols:
                grid.set(by, cx, Cell(hbar, style))
        if 0 <= x + w - 1 < grid.cols:
            grid.set(by, x + w - 1, Cell(br, style))

    # Side borders
    for ry in range(y + 1, y + h - 1):
        if 0 <= ry < grid.rows:
            if 0 <= x < grid.cols:
                grid.set(ry, x, Cell(vbar, style))
            if 0 <= x + w - 1 < grid.cols:
                grid.set(ry, x + w - 1, Cell(vbar, style))


def paint_horizontal_divider(
    grid: Grid,
    x: int,
    y: int,
    w: int,
    *,
    style: Style = DEFAULT_STYLE,
    char: str = "━",
    t: float = 1.0,
) -> int:
    """Draw a horizontal line `w` cells wide, optionally as a partial
    materialize at progress `t` (0.0 = nothing, 1.0 = full line).

    The materialize draws from the CENTER outward in both directions
    so the line "grows" symmetrically — gives a sense of inevitability
    rather than a directional sweep. Returns the number of cells
    actually drawn this frame.

    Used by the compaction boundary animation but also handy for any
    "divider draws in" effect (search results separator, section
    breaks, etc.).
    """
    if w <= 0 or y < 0 or y >= grid.rows:
        return 0
    t = max(0.0, min(1.0, t))
    if t == 0.0:
        return 0
    # Half-width on each side of center, rounded
    half = w // 2
    visible_half = int(round(half * t))
    center = x + half
    left_start = max(x, center - visible_half)
    right_end = min(x + w, center + visible_half + (1 if w % 2 else 0))

    drawn = 0
    for cx in range(left_start, right_end):
        if 0 <= cx < grid.cols:
            grid.set(y, cx, Cell(char, style))
            drawn += 1
    return drawn
