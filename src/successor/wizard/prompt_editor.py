"""PromptEditor — modal multi-line text editor for the config menu.

Used by SuccessorConfig as an overlay when the user opens a MULTILINE
field. Standalone class so the editor doesn't have to know anything
about the config menu (only the config menu knows how to instantiate,
render, and consume the result).

The editor is **Pretext-shaped** in the way that matters for an
editor: per-source-line wrap caching. Each source line is wrapped to
visible chunks at the current text-area width, and the cached result
survives until that line's content changes (or the width changes).
Editing one line invalidates one cache entry; the other 99 lines of a
big prompt keep their cached wraps. Resize is the only operation that
invalidates everything, and resize is rare during text editing.

Cursor coordinates live in **source space** (row, col into self.lines).
Navigation that operates in **visible space** (UP, DOWN, PgUp, PgDn)
maps to/from source coordinates by walking the visible-row index.
This is how every real text editor works — the data model is the
source text, the display is wrapped projection, the cursor knows
where it logically is.

Selection model:
  - `selection_anchor` is None when there's no selection
  - When set, the selected range is anchor → cursor (in either
    direction); paint and editing operations normalize to start ≤ end
  - Shift+arrow keys extend the selection (set anchor first time,
    then move cursor while keeping anchor)
  - Any non-shift navigation clears the selection
  - Esc clears the selection (but doesn't close the editor unless
    pressed twice with no selection)
  - Backspace/Delete/typing with active selection replaces the selected
    range with the new content
  - Ctrl+A selects everything
  - Ctrl+C copies via the OSC 52 callback (passed in by the parent)

Selection paint extends across the FULL width of the text area for
fully-selected interior lines — the empty cells past the source text
get the highlight bg too. This matches Notepad / VS Code / every
modern text editor's multi-line selection look.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..graphemes import (
    delete_next_grapheme,
    delete_prev_grapheme,
    next_grapheme_boundary,
    prev_grapheme_boundary,
)
from ..input.keys import (
    Key,
    KeyEvent,
)
from ..render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    Cell,
    Grid,
    Style,
)
from ..render.paint import (
    BOX_ROUND,
    fill_region,
    paint_box,
    paint_text,
)
from ..render.theme import ThemeVariant


# ─── Soft-wrap primitive ───


@dataclass(frozen=True, slots=True)
class _VisibleChunk:
    """One visible row produced by wrapping a source line.

    source_col_start is the column in the source line where this
    chunk begins. text is the substring of the source line between
    source_col_start and source_col_start + len(text). For empty
    source lines we still produce one chunk with text="" so the row
    is paintable + cursor-addressable.
    """
    source_col_start: int
    text: str

    @property
    def end_col(self) -> int:
        return self.source_col_start + len(self.text)


def _wrap_source_line(line: str, width: int) -> tuple[_VisibleChunk, ...]:
    """Soft-wrap a source line into visible chunks at the given width.

    Greedy: tries to break at the last space before width, falls back
    to a hard break at width if no space exists. Every source character
    appears in exactly one chunk; no chars are dropped or duplicated,
    so cursor positions map cleanly between source and visible space.

    For an empty line, returns a single empty chunk so the row is
    still paintable.

    Pure function — no state, no caching, no I/O. The caller (the
    editor) maintains the per-line cache that wraps this.
    """
    if width <= 0:
        return (_VisibleChunk(0, line),)
    if not line:
        return (_VisibleChunk(0, ""),)

    out: list[_VisibleChunk] = []
    pos = 0
    n = len(line)
    while pos < n:
        end = min(pos + width, n)
        if end < n:
            # Look for the last space within [pos, end] to break at
            break_at = end
            j = end
            while j > pos:
                if line[j - 1] in (" ", "\t"):
                    break_at = j
                    break
                j -= 1
            if break_at == pos:
                # No space found in the chunk — hard break at width
                break_at = end
            end = break_at
        out.append(_VisibleChunk(pos, line[pos:end]))
        pos = end
    return tuple(out)


# ─── Selection helpers ───


def _normalize_selection(
    a: tuple[int, int],
    b: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return (start, end) where start ≤ end in document order."""
    if a[0] < b[0] or (a[0] == b[0] and a[1] <= b[1]):
        return (a, b)
    return (b, a)


def _is_in_selection(
    row: int,
    col: int,
    sel_start: tuple[int, int],
    sel_end: tuple[int, int],
) -> bool:
    """Check whether (row, col) lies in [sel_start, sel_end). Half-open
    on the right so the cursor cell isn't included in the highlight."""
    if row < sel_start[0] or row > sel_end[0]:
        return False
    if row == sel_start[0] and col < sel_start[1]:
        return False
    if row == sel_end[0] and col >= sel_end[1]:
        return False
    return True


# ─── PromptEditor ───


class PromptEditor:
    """Modal multi-line text editor.

    Use:
        editor = PromptEditor(
            initial="...",
            copy_callback=parent.term.copy_to_clipboard,
        )
        # ... input dispatch ...
        editor.handle_key(event)
        # ... render ...
        editor.paint(grid, x=10, y=2, w=80, h=30, theme=variant)
        # ... after each frame ...
        if editor.is_done:
            if editor.result is not None:
                # User saved — commit editor.result
            else:
                # User cancelled
    """

    def __init__(
        self,
        initial: str,
        *,
        copy_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._initial = initial
        self.lines: list[str] = initial.split("\n") if initial else [""]
        if not self.lines:
            self.lines = [""]

        # Cursor in SOURCE coordinates
        self.cursor_row: int = 0
        self.cursor_col: int = 0

        # Selection: None = no selection, otherwise (row, col) anchor
        self.selection_anchor: tuple[int, int] | None = None

        # Vertical scroll in VISIBLE-row space (number of visible
        # chunks above the top of the text area, not source lines)
        self.scroll_offset: int = 0

        # Lifecycle
        self._done: bool = False
        self._result: str | None = None

        # Clipboard callback (parent passes Terminal.copy_to_clipboard)
        self._copy_callback = copy_callback

        # ─── Per-source-line wrap cache ───
        # cache[source_row_idx] = (cached_at_width, tuple of _VisibleChunk)
        # The cache invalidates per-line on edit operations and
        # everything-on-resize (when the painted width differs).
        self._wrap_cache: list[tuple[int, tuple[_VisibleChunk, ...]] | None] = [None] * len(self.lines)

        # Last paint width — used to detect resize and invalidate the
        # cache wholesale. Set on first paint.
        self._last_paint_width: int = -1

    # ─── Public surface ───

    @property
    def is_done(self) -> bool:
        return self._done

    @property
    def result(self) -> str | None:
        return self._result

    @property
    def is_dirty(self) -> bool:
        return "\n".join(self.lines) != self._initial

    @property
    def has_selection(self) -> bool:
        return self.selection_anchor is not None

    def char_count(self) -> int:
        return sum(len(line) for line in self.lines) + max(0, len(self.lines) - 1)

    def get_selection_text(self) -> str:
        """Extract the selected text. Returns empty string if no selection."""
        if self.selection_anchor is None:
            return ""
        start, end = _normalize_selection(
            self.selection_anchor, (self.cursor_row, self.cursor_col)
        )
        if start[0] == end[0]:
            return self.lines[start[0]][start[1] : end[1]]
        # Multi-line selection
        parts: list[str] = [self.lines[start[0]][start[1] :]]
        for r in range(start[0] + 1, end[0]):
            parts.append(self.lines[r])
        parts.append(self.lines[end[0]][: end[1]])
        return "\n".join(parts)

    # ─── Input dispatch ───

    def handle_key(self, event: KeyEvent) -> None:
        if self._done:
            return

        # Ctrl+S commits
        if event.is_ctrl and event.char == "s":
            self._result = "\n".join(self.lines)
            self._done = True
            return

        # Ctrl+A selects all
        if event.is_ctrl and event.char == "a":
            self._select_all()
            return

        # Ctrl+C copies selection (no clear)
        if event.is_ctrl and event.char == "c":
            if self.has_selection and self._copy_callback is not None:
                try:
                    self._copy_callback(self.get_selection_text())
                except Exception:
                    pass
            return

        # Ctrl+X cuts selection
        if event.is_ctrl and event.char == "x":
            if self.has_selection:
                if self._copy_callback is not None:
                    try:
                        self._copy_callback(self.get_selection_text())
                    except Exception:
                        pass
                self._delete_selection()
            return

        # Esc — clears selection if active, otherwise cancels editor
        if event.key == Key.ESC:
            if self.has_selection:
                self.selection_anchor = None
                return
            self._result = None
            self._done = True
            return

        # ─── Navigation (with optional Shift to extend selection) ───
        nav_key = event.key
        if nav_key in (Key.LEFT, Key.RIGHT, Key.UP, Key.DOWN, Key.HOME, Key.END, Key.PG_UP, Key.PG_DOWN):
            if event.is_shift:
                # Extend selection: anchor at current cursor if none yet
                if self.selection_anchor is None:
                    self.selection_anchor = (self.cursor_row, self.cursor_col)
            else:
                # Clear any existing selection
                self.selection_anchor = None
            self._navigate(nav_key)
            return

        # ─── Editing ───
        if event.key == Key.BACKSPACE:
            if self.has_selection:
                self._delete_selection()
            else:
                self._backspace()
            return
        if event.key == Key.DELETE:
            if self.has_selection:
                self._delete_selection()
            else:
                self._delete()
            return
        if event.key == Key.ENTER:
            if self.has_selection:
                self._delete_selection()
            self._insert_newline()
            return

        # Tab inserts 2 spaces (the most common editor convention for
        # narrow text areas; not configurable for v0)
        if event.key == Key.TAB:
            if self.has_selection:
                self._delete_selection()
            self._insert_str("  ")
            return

        # Printable input
        if event.is_char and event.char and not event.is_ctrl and not event.is_alt:
            if self.has_selection:
                self._delete_selection()
            for ch in event.char:
                if ch == "\n":
                    self._insert_newline()
                elif ord(ch) >= 0x20:
                    self._insert_char(ch)

    # ─── Cursor navigation ───

    def _navigate(self, key: Key) -> None:
        if key == Key.LEFT:
            self._cursor_left()
        elif key == Key.RIGHT:
            self._cursor_right()
        elif key == Key.UP:
            self._cursor_up_visible()
        elif key == Key.DOWN:
            self._cursor_down_visible()
        elif key == Key.HOME:
            self.cursor_col = 0
        elif key == Key.END:
            self.cursor_col = len(self.lines[self.cursor_row])
        elif key == Key.PG_UP:
            for _ in range(10):
                self._cursor_up_visible()
        elif key == Key.PG_DOWN:
            for _ in range(10):
                self._cursor_down_visible()

    def _cursor_left(self) -> None:
        if self.cursor_col > 0:
            line = self.lines[self.cursor_row]
            self.cursor_col = prev_grapheme_boundary(line, self.cursor_col)
        elif self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = len(self.lines[self.cursor_row])

    def _cursor_right(self) -> None:
        if self.cursor_col < len(self.lines[self.cursor_row]):
            line = self.lines[self.cursor_row]
            self.cursor_col = next_grapheme_boundary(line, self.cursor_col)
        elif self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = 0

    def _cursor_up_visible(self) -> None:
        """Move cursor up by ONE VISIBLE row.

        Walks the cached wrap to find which visible chunk contains
        the cursor, computes the visual column within that chunk,
        moves to the previous visible chunk (which might be a
        previous chunk in the SAME source row, or the last chunk in
        the previous source row), and snaps the source col to the
        same visual column in the new chunk.
        """
        chunk_idx = self._cursor_chunk_index()
        if chunk_idx <= 0:
            self.cursor_row = 0
            self.cursor_col = 0
            return

        # Find the visual col in the cursor's current chunk
        cur_chunk = self._chunk_at_index(chunk_idx)
        if cur_chunk is None:
            return
        visual_col = self.cursor_col - cur_chunk[1].source_col_start

        # Move to the previous chunk
        new_chunk_idx = chunk_idx - 1
        new = self._chunk_at_index(new_chunk_idx)
        if new is None:
            return
        new_row, new_chunk = new
        # Snap visual col to within the new chunk's source range
        target_col = new_chunk.source_col_start + min(visual_col, len(new_chunk.text))
        self.cursor_row = new_row
        self.cursor_col = target_col

    def _cursor_down_visible(self) -> None:
        """Move cursor down by ONE VISIBLE row. Mirror of _cursor_up_visible."""
        chunk_idx = self._cursor_chunk_index()
        total = self._total_chunk_count()
        if chunk_idx >= total - 1:
            self.cursor_row = len(self.lines) - 1
            self.cursor_col = len(self.lines[self.cursor_row])
            return

        cur_chunk = self._chunk_at_index(chunk_idx)
        if cur_chunk is None:
            return
        visual_col = self.cursor_col - cur_chunk[1].source_col_start

        new_chunk_idx = chunk_idx + 1
        new = self._chunk_at_index(new_chunk_idx)
        if new is None:
            return
        new_row, new_chunk = new
        target_col = new_chunk.source_col_start + min(visual_col, len(new_chunk.text))
        self.cursor_row = new_row
        self.cursor_col = target_col

    def _cursor_chunk_index(self) -> int:
        """Walk the wraps to find the global visible-chunk index of the cursor."""
        idx = 0
        for r in range(self.cursor_row):
            chunks = self._chunks_for_line(r)
            idx += len(chunks)
        chunks = self._chunks_for_line(self.cursor_row)
        for i, chunk in enumerate(chunks):
            if chunk.source_col_start <= self.cursor_col < chunk.source_col_start + len(chunk.text):
                return idx + i
            # Cursor at the end of the line lives in the LAST chunk
            if i == len(chunks) - 1 and self.cursor_col == chunk.source_col_start + len(chunk.text):
                return idx + i
        return idx

    def _chunk_at_index(self, target_idx: int) -> tuple[int, _VisibleChunk] | None:
        """Find the (source_row, chunk) at the given global visible-chunk index."""
        idx = 0
        for r in range(len(self.lines)):
            chunks = self._chunks_for_line(r)
            if idx + len(chunks) > target_idx:
                return (r, chunks[target_idx - idx])
            idx += len(chunks)
        return None

    def _total_chunk_count(self) -> int:
        return sum(len(self._chunks_for_line(r)) for r in range(len(self.lines)))

    # ─── Wrap cache ───

    def _chunks_for_line(self, source_row: int) -> tuple[_VisibleChunk, ...]:
        """Return the cached wrap for a source row, recomputing if needed."""
        if source_row < 0 or source_row >= len(self.lines):
            return ()
        cached = self._wrap_cache[source_row]
        if (
            cached is not None
            and cached[0] == self._last_paint_width
            and self._last_paint_width > 0
        ):
            return cached[1]
        # Need to recompute. Use a fallback width if we haven't painted
        # yet (1000 effectively means "no wrap"). The first paint with
        # the real width will rebuild the cache.
        width = self._last_paint_width if self._last_paint_width > 0 else 1000
        chunks = _wrap_source_line(self.lines[source_row], width)
        self._wrap_cache[source_row] = (width, chunks)
        return chunks

    def _invalidate_line(self, source_row: int) -> None:
        if 0 <= source_row < len(self._wrap_cache):
            self._wrap_cache[source_row] = None

    def _invalidate_all(self) -> None:
        self._wrap_cache = [None] * len(self.lines)

    # ─── Selection operations ───

    def _select_all(self) -> None:
        self.selection_anchor = (0, 0)
        self.cursor_row = len(self.lines) - 1
        self.cursor_col = len(self.lines[-1])

    def _delete_selection(self) -> None:
        """Delete the currently-selected range, leaving cursor at start."""
        if self.selection_anchor is None:
            return
        start, end = _normalize_selection(
            self.selection_anchor, (self.cursor_row, self.cursor_col)
        )
        if start == end:
            self.selection_anchor = None
            return

        if start[0] == end[0]:
            # Single-line selection
            line = self.lines[start[0]]
            self.lines[start[0]] = line[: start[1]] + line[end[1] :]
            self._invalidate_line(start[0])
        else:
            # Multi-line selection — splice
            first = self.lines[start[0]][: start[1]]
            last = self.lines[end[0]][end[1] :]
            self.lines[start[0]] = first + last
            del self.lines[start[0] + 1 : end[0] + 1]
            # The wrap cache needs partial invalidation: line start[0] is
            # invalidated, and lines after the deleted range shift.
            # Easiest correct thing: rebuild the cache wholesale.
            self._wrap_cache = [None] * len(self.lines)

        self.cursor_row = start[0]
        self.cursor_col = start[1]
        self.selection_anchor = None

    # ─── Edit operations ───

    def _insert_str(self, s: str) -> None:
        for ch in s:
            if ch == "\n":
                self._insert_newline()
            else:
                self._insert_char(ch)

    def _insert_char(self, ch: str) -> None:
        line = self.lines[self.cursor_row]
        self.lines[self.cursor_row] = line[: self.cursor_col] + ch + line[self.cursor_col :]
        self.cursor_col += 1
        self._invalidate_line(self.cursor_row)

    def _insert_newline(self) -> None:
        line = self.lines[self.cursor_row]
        before = line[: self.cursor_col]
        after = line[self.cursor_col :]
        self.lines[self.cursor_row] = before
        self.lines.insert(self.cursor_row + 1, after)
        self.cursor_row += 1
        self.cursor_col = 0
        # Insertion shifts subsequent lines — easier to rebuild than to
        # remap, and only fires on newline insertion (not per-keystroke)
        self._wrap_cache = [None] * len(self.lines)

    def _backspace(self) -> None:
        if self.cursor_col > 0:
            line = self.lines[self.cursor_row]
            self.lines[self.cursor_row], self.cursor_col = delete_prev_grapheme(
                line,
                self.cursor_col,
            )
            self._invalidate_line(self.cursor_row)
        elif self.cursor_row > 0:
            prev_line = self.lines[self.cursor_row - 1]
            cur_line = self.lines[self.cursor_row]
            new_col = len(prev_line)
            self.lines[self.cursor_row - 1] = prev_line + cur_line
            del self.lines[self.cursor_row]
            self.cursor_row -= 1
            self.cursor_col = new_col
            self._wrap_cache = [None] * len(self.lines)

    def _delete(self) -> None:
        line = self.lines[self.cursor_row]
        if self.cursor_col < len(line):
            self.lines[self.cursor_row], self.cursor_col = delete_next_grapheme(
                line,
                self.cursor_col,
            )
            self._invalidate_line(self.cursor_row)
        elif self.cursor_row < len(self.lines) - 1:
            next_line = self.lines[self.cursor_row + 1]
            self.lines[self.cursor_row] = line + next_line
            del self.lines[self.cursor_row + 1]
            self._wrap_cache = [None] * len(self.lines)

    # ─── Paint ───

    def paint(
        self,
        grid: Grid,
        *,
        x: int,
        y: int,
        w: int,
        h: int,
        theme: ThemeVariant,
    ) -> None:
        """Render the editor as a modal box at (x, y, w, h)."""
        if w < 30 or h < 8:
            return

        # Box background + border
        border_style = Style(fg=theme.accent, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_box(
            grid, x, y, w, h,
            style=border_style, fill_style=fill_style, chars=BOX_ROUND,
        )

        # Title bar
        dirty_marker = " *" if self.is_dirty else ""
        title = f" edit system prompt{dirty_marker} "
        paint_text(
            grid, title, x + 2, y,
            style=Style(fg=theme.bg, bg=theme.accent, attrs=ATTR_BOLD),
        )

        # Right-anchored info: line N/M · char count + selection size if any
        sel_info = ""
        if self.has_selection:
            sel_text = self.get_selection_text()
            sel_info = f" · {len(sel_text)} sel"
        info = f" line {self.cursor_row + 1}/{len(self.lines)} · {self.char_count()} chars{sel_info} "
        info_x = x + w - len(info) - 2
        if info_x > x + len(title) + 2:
            paint_text(
                grid, info, info_x, y,
                style=Style(fg=theme.bg, bg=theme.accent, attrs=ATTR_BOLD),
            )

        # Footer keybinds
        if self.has_selection:
            footer = " ↑↓←→ extend (shift) · ⌃C copy · ⌃X cut · ⌃A select all · ⌃S save · esc clear "
        else:
            footer = " ↑↓←→ navigate · shift+arrows select · ⌃A all · ⌃S save · esc cancel "
        if len(footer) <= w - 4:
            fx = x + (w - len(footer)) // 2
            paint_text(
                grid, footer, fx, y + h - 1,
                style=Style(fg=theme.bg, bg=theme.accent_warm, attrs=ATTR_BOLD),
            )

        # ─── Text area layout ───
        gutter_w = 5  # " 999 "
        sep_w = 1
        text_x = x + 2 + gutter_w + sep_w
        text_y = y + 1
        text_w = max(1, x + w - 2 - text_x)
        text_h = max(1, h - 2)

        # Detect width change → invalidate the wrap cache
        if text_w != self._last_paint_width:
            self._last_paint_width = text_w
            self._invalidate_all()

        # Build the visible-row list — flat sequence of (source_row, chunk)
        visible_rows: list[tuple[int, _VisibleChunk]] = []
        for r in range(len(self.lines)):
            for chunk in self._chunks_for_line(r):
                visible_rows.append((r, chunk))

        # ─── Auto-scroll vertically to keep cursor in view ───
        cursor_visible_idx = self._cursor_chunk_index()
        if cursor_visible_idx < self.scroll_offset:
            self.scroll_offset = cursor_visible_idx
        elif cursor_visible_idx >= self.scroll_offset + text_h:
            self.scroll_offset = cursor_visible_idx - text_h + 1
        if self.scroll_offset < 0:
            self.scroll_offset = 0
        if self.scroll_offset >= len(visible_rows):
            self.scroll_offset = max(0, len(visible_rows) - 1)

        # ─── Selection range (normalized) ───
        sel_start: tuple[int, int] | None = None
        sel_end: tuple[int, int] | None = None
        if self.selection_anchor is not None:
            sel_start, sel_end = _normalize_selection(
                self.selection_anchor, (self.cursor_row, self.cursor_col)
            )

        # Track which source row the cursor is in (for line number highlighting)
        cursor_source_row = self.cursor_row

        # ─── Paint visible rows ───
        for vi in range(text_h):
            row_idx = self.scroll_offset + vi
            if row_idx >= len(visible_rows):
                break
            source_row, chunk = visible_rows[row_idx]
            screen_y = text_y + vi

            # Line number — only show on the FIRST visible chunk of a
            # source line (continuation chunks just show "      │")
            is_first_chunk_of_line = chunk.source_col_start == 0
            if is_first_chunk_of_line:
                num_text = f"{source_row + 1:>4} "
            else:
                num_text = "     "
            num_style = Style(
                fg=theme.fg_subtle if source_row != cursor_source_row else theme.accent_warm,
                bg=theme.bg_input,
                attrs=ATTR_DIM if source_row != cursor_source_row else ATTR_BOLD,
            )
            paint_text(grid, num_text, x + 2, screen_y, style=num_style)

            # Separator
            paint_text(
                grid, "│", x + 2 + gutter_w, screen_y,
                style=Style(fg=theme.fg_subtle, bg=theme.bg_input),
            )

            # ─── Selection-aware text painting ───
            # Walk each character in the chunk and paint with the
            # appropriate bg color (highlight if selected, normal
            # otherwise). Also handles the trailing-cells highlight
            # for fully-selected interior rows.
            self._paint_chunk_with_selection(
                grid, chunk, source_row,
                text_x=text_x, screen_y=screen_y, text_w=text_w,
                sel_start=sel_start, sel_end=sel_end,
                theme=theme,
            )

        # ─── Paint cursor ───
        # Find which screen row the cursor is on
        cursor_screen_row = cursor_visible_idx - self.scroll_offset
        if 0 <= cursor_screen_row < text_h:
            # Find the chunk that contains the cursor
            cursor_chunk = visible_rows[cursor_visible_idx]
            chunk_obj = cursor_chunk[1]
            visual_col = self.cursor_col - chunk_obj.source_col_start
            cursor_screen_x = text_x + visual_col
            cy = text_y + cursor_screen_row
            if cursor_screen_x < text_x + text_w:
                # Read the char under the cursor (or space if past EOL)
                line = self.lines[self.cursor_row]
                ch = line[self.cursor_col] if self.cursor_col < len(line) else " "
                cursor_style = Style(
                    fg=theme.bg_input, bg=theme.fg, attrs=ATTR_BOLD,
                )
                grid.set(cy, cursor_screen_x, Cell(ch, cursor_style))

    def _paint_chunk_with_selection(
        self,
        grid: Grid,
        chunk: _VisibleChunk,
        source_row: int,
        *,
        text_x: int,
        screen_y: int,
        text_w: int,
        sel_start: tuple[int, int] | None,
        sel_end: tuple[int, int] | None,
        theme: ThemeVariant,
    ) -> None:
        """Paint one visible chunk, applying the selection highlight per cell.

        For chars within the selection range: paint with selection bg.
        For chars outside: paint with the normal bg.
        For fully-selected interior source rows, the cells past the end
        of the chunk's text ALSO get the selection bg (extending the
        highlight all the way to the right edge of the text area).
        """
        normal_bg = theme.bg_input
        normal_fg = theme.fg
        sel_bg = theme.accent
        sel_fg = theme.bg

        # Walk each char in the chunk
        for vi, ch in enumerate(chunk.text):
            if vi >= text_w:
                break
            source_col = chunk.source_col_start + vi
            in_sel = (
                sel_start is not None
                and sel_end is not None
                and _is_in_selection(source_row, source_col, sel_start, sel_end)
            )
            cell_bg = sel_bg if in_sel else normal_bg
            cell_fg = sel_fg if in_sel else normal_fg
            paint_text(
                grid, ch, text_x + vi, screen_y,
                style=Style(fg=cell_fg, bg=cell_bg),
            )

        # Trailing-cell highlight for fully-selected interior rows.
        # A row is "fully selected through its end" if the selection
        # spans past this chunk's last source col. We paint the empty
        # cells past the chunk text with the selection bg so the
        # highlight extends to the right edge of the text area.
        if sel_start is not None and sel_end is not None:
            # The trailing-cells highlight kicks in for any visible chunk
            # whose source row is between sel_start[0] and sel_end[0]
            # exclusive — these are the "interior" rows whose entire
            # source content (and trailing whitespace) is selected.
            row_is_interior = (
                source_row > sel_start[0]
                and source_row < sel_end[0]
            )
            if row_is_interior:
                # Fill cells from chunk end to text area end
                trailing_x_start = text_x + len(chunk.text)
                trailing_w = text_w - len(chunk.text)
                if trailing_w > 0:
                    fill_region(
                        grid,
                        trailing_x_start, screen_y, trailing_w, 1,
                        style=Style(fg=sel_fg, bg=sel_bg),
                    )
