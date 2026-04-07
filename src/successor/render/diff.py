"""Layer 5 — diff two Grids and emit minimal ANSI.

This is the ONLY module in Successor that produces terminal-bound bytes.
Nothing else in the codebase should ever directly write SGR or cursor
codes. If you find yourself wanting to, the answer is: paint into the
grid, let the diff layer handle it.

Algorithm:
  1. Walk both grids in row-major order.
  2. For each row, find runs of cells where curr != prev.
  3. For each dirty run:
       - Move the cursor to the run's start
       - For each cell in the run, emit SGR if its style differs from
         "current style", then emit the cell's character
  4. The "current style" is tracked across the WHOLE frame so that
     consecutive cells with the same style don't re-emit SGR codes.

When prev is None or has different dimensions, the entire screen is
treated as dirty (full clear-and-redraw).
"""

from __future__ import annotations

from .cells import (
    ATTR_BOLD,
    ATTR_DIM,
    ATTR_ITALIC,
    ATTR_REVERSE,
    ATTR_STRIKE,
    ATTR_UNDERLINE,
    Grid,
    Style,
)

CSI = "\x1b["
SGR_RESET = CSI + "0m"
CLEAR_SCREEN = CSI + "2J"
HOME = CSI + "H"


def _sgr(style: Style) -> str:
    """Build a complete SGR sequence for the given style.

    We always start with `0` so the sequence is self-contained: it doesn't
    depend on the previous SGR state. This costs a few bytes per state
    change but makes the wire output deterministic and easy to reason about.
    """
    parts: list[str] = ["0"]
    a = style.attrs
    if a & ATTR_BOLD:
        parts.append("1")
    if a & ATTR_DIM:
        parts.append("2")
    if a & ATTR_ITALIC:
        parts.append("3")
    if a & ATTR_UNDERLINE:
        parts.append("4")
    if a & ATTR_REVERSE:
        parts.append("7")
    if a & ATTR_STRIKE:
        parts.append("9")
    if style.fg is not None:
        r = (style.fg >> 16) & 0xFF
        g = (style.fg >> 8) & 0xFF
        b = style.fg & 0xFF
        parts.append(f"38;2;{r};{g};{b}")
    if style.bg is not None:
        r = (style.bg >> 16) & 0xFF
        g = (style.bg >> 8) & 0xFF
        b = style.bg & 0xFF
        parts.append(f"48;2;{r};{g};{b}")
    return CSI + ";".join(parts) + "m"


def _move(row: int, col: int) -> str:
    # CSI uses 1-indexed coordinates; our grid is 0-indexed.
    return f"{CSI}{row + 1};{col + 1}H"


def render_full(curr: Grid) -> str:
    """Full screen render — used on first frame and after resize."""
    out: list[str] = [CLEAR_SCREEN, HOME]
    last_style: Style | None = None
    for r in range(curr.rows):
        out.append(_move(r, 0))
        for c in range(curr.cols):
            cell = curr.at(r, c)
            if cell.wide_tail:
                continue
            if cell.style != last_style:
                out.append(_sgr(cell.style))
                last_style = cell.style
            out.append(cell.char if cell.char else " ")
    out.append(SGR_RESET)
    return "".join(out)


def diff_frames(prev: Grid | None, curr: Grid) -> str:
    """Emit ANSI for the minimum cells that changed between prev and curr.

    If prev is None or sized differently from curr, falls back to render_full.
    """
    if prev is None or prev.rows != curr.rows or prev.cols != curr.cols:
        return render_full(curr)

    out: list[str] = []
    last_style: Style | None = None

    for r in range(curr.rows):
        c = 0
        cols = curr.cols
        while c < cols:
            if curr.at(r, c) == prev.at(r, c):
                c += 1
                continue
            # Found the start of a dirty run on this row.
            run_start = c
            while c < cols and curr.at(r, c) != prev.at(r, c):
                c += 1
            # Move cursor to run start once.
            out.append(_move(r, run_start))
            # Emit cells in [run_start, c).
            for cc in range(run_start, c):
                cell = curr.at(r, cc)
                if cell.wide_tail:
                    continue
                if cell.style != last_style:
                    out.append(_sgr(cell.style))
                    last_style = cell.style
                out.append(cell.char if cell.char else " ")
    if out:
        out.append(SGR_RESET)
    return "".join(out)
