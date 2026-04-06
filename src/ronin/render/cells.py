"""Cell, Style, and Grid — the in-memory virtual screen.

The Grid is the renderer's central data structure: a 2D array of Cell
objects representing exactly what should appear on the terminal. Layers
above mutate the grid; the diff layer compares two grids and emits the
minimum ANSI to bring the terminal from one to the other.

Nothing in this file touches the terminal.
"""

from __future__ import annotations

from dataclasses import dataclass

# Style attribute bitmask
ATTR_BOLD = 1 << 0
ATTR_DIM = 1 << 1
ATTR_ITALIC = 1 << 2
ATTR_UNDERLINE = 1 << 3
ATTR_REVERSE = 1 << 4
ATTR_STRIKE = 1 << 5


@dataclass(frozen=True, slots=True)
class Style:
    """Immutable cell style.

    fg / bg are 24-bit packed RGB ints (0xRRGGBB) or None for "terminal default".
    attrs is a bitmask of ATTR_* flags.
    """
    fg: int | None = None
    bg: int | None = None
    attrs: int = 0


DEFAULT_STYLE = Style()


@dataclass(slots=True)
class Cell:
    """A single screen cell.

    char:        the visible character (may include combining marks appended)
    style:       the cell's Style
    wide_tail:   True if this cell is the trailing half of a width-2 grapheme;
                 the diff writer skips these so we don't double-write the char.
    """
    char: str = " "
    style: Style = DEFAULT_STYLE
    wide_tail: bool = False

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Cell):
            return NotImplemented
        return (
            self.char == other.char
            and self.style == other.style
            and self.wide_tail == other.wide_tail
        )

    def __hash__(self) -> int:
        return hash((self.char, self.style, self.wide_tail))


class Grid:
    """A rows × cols grid of Cell objects.

    The grid is mutated freely by paint operations. The diff layer treats it
    as immutable input and produces a delta against the previous frame.

    Out-of-bounds writes via .set() are silently ignored — paint operations
    are responsible for clipping but the grid won't crash if they don't.
    """
    __slots__ = ("rows", "cols", "_cells")

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self._cells: list[list[Cell]] = [
            [Cell() for _ in range(cols)] for _ in range(rows)
        ]

    def at(self, r: int, c: int) -> Cell:
        return self._cells[r][c]

    def set(self, r: int, c: int, cell: Cell) -> None:
        if 0 <= r < self.rows and 0 <= c < self.cols:
            self._cells[r][c] = cell

    def clear(self, style: Style = DEFAULT_STYLE) -> None:
        """Reset every cell to a blank with the given style.

        Allocates one Cell and shares it — Cell is small and never mutated
        in place by paint operations (they always replace via .set()), so
        sharing is safe.
        """
        blank = Cell(" ", style)
        for row in self._cells:
            for c in range(self.cols):
                row[c] = blank

    def row(self, r: int) -> list[Cell]:
        return self._cells[r]
