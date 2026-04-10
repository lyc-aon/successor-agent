"""Overlay painters for chat autocomplete and help surfaces."""

from __future__ import annotations

import time

from .cells import ATTR_BOLD, ATTR_DIM, Grid, Style
from .chat_frame import HitBox
from .paint import fill_region, paint_box, paint_text
from .text import ease_out_cubic, hard_wrap, lerp_rgb
from .theme import ThemeVariant


def blank_dropdown_rows(grid: Grid, theme: ThemeVariant, box_y: int, box_h: int) -> None:
    """Blank the rows a dropdown occupies so chat content does not leak around it."""
    for blank_y in range(box_y, box_y + box_h):
        if 0 <= blank_y < grid.rows:
            fill_region(
                grid, 0, blank_y, grid.cols, 1,
                style=Style(bg=theme.bg),
            )


def paint_name_mode(
    grid: Grid,
    theme: ThemeVariant,
    input_y: int,
    state: object,
    *,
    prompt_width: int,
) -> list[HitBox]:
    cols = grid.cols
    max_visible = max(1, min(len(state.matches), max(3, input_y - 3)))
    visible = state.matches[:max_visible]

    cmd_col_w = max(len(f"/{c.name}") for c in visible)
    desc_col_w = max((len(c.description) for c in visible), default=0)
    hint_col_w = max((len(c.args_hint) for c in visible), default=0)

    inner_w = cmd_col_w + 2 + desc_col_w
    if hint_col_w > 0:
        inner_w += 2 + hint_col_w
    inner_w = max(inner_w, 36)
    box_w = min(inner_w + 4, cols - 2)
    box_h = max_visible + 2

    box_x = max(0, prompt_width)
    box_y = input_y - box_h - 1
    if box_y < 1:
        box_y = 1
        box_h = min(box_h, input_y - box_y - 1)
        if box_h < 3:
            return []

    blank_dropdown_rows(grid, theme, box_y, box_h)

    border_style = Style(fg=theme.accent_warm, bg=theme.bg_input, attrs=ATTR_BOLD)
    fill_style = Style(fg=theme.fg, bg=theme.bg_input)
    paint_box(
        grid, box_x, box_y, box_w, box_h,
        style=border_style, fill_style=fill_style,
    )

    hitboxes: list[HitBox] = []
    item_x = box_x + 2
    for i, cmd in enumerate(visible):
        row_y = box_y + 1 + i
        if row_y >= box_y + box_h - 1:
            break

        is_selected = i == state.selected
        row_bg = theme.accent if is_selected else theme.bg_input
        row_fg = theme.bg if is_selected else theme.fg
        dim_fg = theme.bg if is_selected else theme.fg_dim
        subtle_fg = theme.bg if is_selected else theme.fg_subtle

        fill_region(
            grid, box_x + 1, row_y, box_w - 2, 1,
            style=Style(bg=row_bg),
        )

        cmd_text = f"/{cmd.name}"
        paint_text(
            grid, cmd_text, item_x, row_y,
            style=Style(fg=row_fg, bg=row_bg, attrs=ATTR_BOLD),
        )

        desc_x = item_x + cmd_col_w + 2
        paint_text(
            grid, cmd.description, desc_x, row_y,
            style=Style(fg=dim_fg, bg=row_bg),
        )

        if cmd.args_hint:
            hint_x = desc_x + desc_col_w + 2
            paint_text(
                grid, cmd.args_hint, hint_x, row_y,
                style=Style(fg=subtle_fg, bg=row_bg, attrs=ATTR_DIM),
            )

        hitboxes.append(HitBox(box_x + 1, row_y, box_w - 2, 1, f"slash:{cmd.name}"))
    return hitboxes


def paint_arg_mode(
    grid: Grid,
    theme: ThemeVariant,
    input_y: int,
    state: object,
    *,
    prompt_width: int,
) -> list[HitBox]:
    cols = grid.cols
    cmd = state.command

    max_visible = max(1, min(len(state.matches), max(3, input_y - 4)))
    visible = state.matches[:max_visible]

    header = f" /{cmd.name} · {cmd.description} "
    arg_col_w = max(len(a) for a in visible)
    inner_w = max(len(header) - 2, arg_col_w + 4)
    inner_w = max(inner_w, 36)
    box_w = min(inner_w + 4, cols - 2)
    box_h = 1 + max_visible + 2

    box_x = max(0, prompt_width)
    box_y = input_y - box_h - 1
    if box_y < 1:
        box_y = 1
        box_h = min(box_h, input_y - box_y - 1)
        if box_h < 4:
            return []

    blank_dropdown_rows(grid, theme, box_y, box_h)

    border_style = Style(fg=theme.accent, bg=theme.bg_input, attrs=ATTR_BOLD)
    fill_style = Style(fg=theme.fg, bg=theme.bg_input)
    paint_box(
        grid, box_x, box_y, box_w, box_h,
        style=border_style, fill_style=fill_style,
    )

    header_y = box_y + 1
    if header_y < box_y + box_h - 1:
        fill_region(
            grid, box_x + 1, header_y, box_w - 2, 1,
            style=Style(bg=theme.bg_footer),
        )
        paint_text(
            grid, header, box_x + 2, header_y,
            style=Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_BOLD),
        )

    hitboxes: list[HitBox] = []
    item_x = box_x + 2
    first_item_y = box_y + 2
    for i, arg in enumerate(visible):
        row_y = first_item_y + i
        if row_y >= box_y + box_h - 1:
            break

        is_selected = i == state.selected
        row_bg = theme.accent if is_selected else theme.bg_input
        row_fg = theme.bg if is_selected else theme.fg
        dim_fg = theme.bg if is_selected else theme.fg_dim

        fill_region(
            grid, box_x + 1, row_y, box_w - 2, 1,
            style=Style(bg=row_bg),
        )

        paint_text(
            grid, arg, item_x, row_y,
            style=Style(fg=row_fg, bg=row_bg, attrs=ATTR_BOLD),
        )
        if state.partial:
            hint_x = item_x + arg_col_w + 2
            hint_text = f"matched '{state.partial}'"
            paint_text(
                grid, hint_text, hint_x, row_y,
                style=Style(fg=dim_fg, bg=row_bg, attrs=ATTR_DIM),
            )

        hitboxes.append(HitBox(box_x + 1, row_y, box_w - 2, 1, f"arg:{arg}"))
    return hitboxes


def paint_no_matches(
    grid: Grid,
    theme: ThemeVariant,
    input_y: int,
    state: object,
    *,
    prompt_width: int,
) -> None:
    cols = grid.cols

    lines: list[str] = [state.text]
    if state.mode == "name":
        lines.append("type / alone to see all commands")
    elif state.mode == "arg" and state.valid_options:
        valid = ", ".join(state.valid_options)
        lines.append(f"valid: {valid}")

    inner_w = max(len(line) for line in lines)
    inner_w = max(inner_w, 32)
    box_w = min(inner_w + 4, cols - 2)
    box_h = len(lines) + 2

    box_x = max(0, prompt_width)
    box_y = input_y - box_h - 1
    if box_y < 1:
        return

    blank_dropdown_rows(grid, theme, box_y, box_h)

    border_style = Style(fg=theme.fg_dim, bg=theme.bg_input)
    fill_style = Style(fg=theme.fg_dim, bg=theme.bg_input)
    paint_box(
        grid, box_x, box_y, box_w, box_h,
        style=border_style, fill_style=fill_style,
    )

    for i, text in enumerate(lines):
        row_y = box_y + 1 + i
        if row_y >= box_y + box_h - 1:
            break
        fg = theme.fg_dim if i == 0 else theme.fg_subtle
        paint_text(
            grid, text, box_x + 2, row_y,
            style=Style(fg=fg, bg=theme.bg_input, attrs=ATTR_DIM),
        )


def paint_help_overlay(
    grid: Grid,
    theme: ThemeVariant,
    *,
    opened_at: float,
    sections: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
    title_text: str = "successor · keybindings",
) -> None:
    rows, cols = grid.rows, grid.cols
    if rows < 8 or cols < 50:
        return

    key_col_w = max(
        max(len(key) for key, _ in entries)
        for _, entries in sections
    )
    desc_col_w = max(
        max(len(desc) for _, desc in entries)
        for _, entries in sections
    )
    inner_w = max(key_col_w + 3 + desc_col_w, len(title_text) + 4)
    box_w = min(inner_w + 6, cols - 4)

    sections_h = 0
    for _, entries in sections:
        sections_h += 1 + len(entries)
    inner_h = 1 + 1 + sections_h + 1 + 1
    box_h = min(inner_h + 2, rows - 2)

    box_x = max(0, (cols - box_w) // 2)
    box_y = max(0, (rows - box_h) // 2)

    elapsed = time.monotonic() - opened_at
    fade_t = ease_out_cubic(min(1.0, elapsed / 0.18))

    def fade(target: int) -> int:
        return lerp_rgb(theme.bg, target, fade_t)

    border_color = fade(theme.accent)
    border_style = Style(fg=border_color, bg=theme.bg_input, attrs=ATTR_BOLD)
    fill_style = Style(fg=fade(theme.fg), bg=theme.bg_input)
    paint_box(
        grid, box_x, box_y, box_w, box_h,
        style=border_style, fill_style=fill_style,
    )

    title_y = box_y + 1
    if title_y < box_y + box_h - 1:
        tx = box_x + (box_w - len(title_text)) // 2
        paint_text(
            grid, title_text, tx, title_y,
            style=Style(fg=fade(theme.accent), bg=theme.bg_input, attrs=ATTR_BOLD),
        )

    cur_y = title_y + 2
    section_header_color = fade(theme.fg_dim)
    key_color = fade(theme.accent_warm)
    desc_color = fade(theme.fg)

    for section_name, entries in sections:
        if cur_y >= box_y + box_h - 2:
            break
        paint_text(
            grid, f"  {section_name}",
            box_x + 2, cur_y,
            style=Style(
                fg=section_header_color,
                bg=theme.bg_input,
                attrs=ATTR_DIM,
            ),
        )
        cur_y += 1

        for key, desc in entries:
            if cur_y >= box_y + box_h - 2:
                break
            key_padded = key.rjust(key_col_w)
            paint_text(
                grid, key_padded,
                box_x + 4, cur_y,
                style=Style(fg=key_color, bg=theme.bg_input, attrs=ATTR_BOLD),
            )
            paint_text(
                grid, desc,
                box_x + 4 + key_col_w + 3, cur_y,
                style=Style(fg=desc_color, bg=theme.bg_input),
            )
            cur_y += 1

    hint_y = box_y + box_h - 2
    if box_y + 1 <= hint_y < box_y + box_h - 1:
        hint = "press any key to close"
        hx = box_x + (box_w - len(hint)) // 2
        paint_text(
            grid, hint, hx, hint_y,
            style=Style(
                fg=fade(theme.fg_subtle),
                bg=theme.bg_input,
                attrs=ATTR_DIM,
            ),
        )


def _history_snippet(text: str, width: int) -> str:
    collapsed = " ".join(
        text.replace("\n", " ↵ ").replace("\r", "").split()
    ).strip()
    if not collapsed:
        collapsed = "∅"
    if width <= 1 or len(collapsed) <= width:
        return collapsed[:width]
    return collapsed[: max(1, width - 1)] + "…"


def paint_history_overlay(
    grid: Grid,
    theme: ThemeVariant,
    *,
    opened_at: float,
    entries: list[str],
    selected: int,
    query: str,
    draft: str,
    title_text: str = "history browser",
) -> list[HitBox]:
    rows, cols = grid.rows, grid.cols
    if rows < 12 or cols < 64:
        return []

    box_w = min(max(72, cols - 18), cols - 4)
    box_h = min(max(14, rows - 6), rows - 2)
    box_x = max(0, (cols - box_w) // 2)
    box_y = max(0, (rows - box_h) // 2)

    elapsed = time.monotonic() - opened_at
    fade_t = ease_out_cubic(min(1.0, elapsed / 0.18))

    def fade(target: int) -> int:
        return lerp_rgb(theme.bg, target, fade_t)

    border_style = Style(
        fg=fade(theme.accent_warm),
        bg=theme.bg_input,
        attrs=ATTR_BOLD,
    )
    fill_style = Style(fg=fade(theme.fg), bg=theme.bg_input)
    paint_box(
        grid,
        box_x,
        box_y,
        box_w,
        box_h,
        style=border_style,
        fill_style=fill_style,
    )

    title_y = box_y + 1
    if title_y < box_y + box_h - 1:
        tx = box_x + (box_w - len(title_text)) // 2
        paint_text(
            grid,
            title_text,
            tx,
            title_y,
            style=Style(
                fg=fade(theme.accent),
                bg=theme.bg_input,
                attrs=ATTR_BOLD,
            ),
        )

    meta_y = title_y + 1
    query_text = query if query else "all entries"
    draft_text = _history_snippet(draft, max(12, box_w - 34))
    meta = f" filter: {query_text} "
    draft_meta = f" draft: {draft_text} "
    if meta_y < box_y + box_h - 1:
        fill_region(
            grid,
            box_x + 1,
            meta_y,
            box_w - 2,
            1,
            style=Style(bg=theme.bg_footer),
        )
        paint_text(
            grid,
            meta,
            box_x + 2,
            meta_y,
            style=Style(
                fg=fade(theme.fg_dim),
                bg=theme.bg_footer,
                attrs=ATTR_BOLD,
            ),
        )
        if len(draft_meta) < box_w - 8:
            dx = box_x + box_w - 2 - len(draft_meta)
            paint_text(
                grid,
                draft_meta,
                dx,
                meta_y,
                style=Style(
                    fg=fade(theme.fg_subtle),
                    bg=theme.bg_footer,
                    attrs=ATTR_DIM,
                ),
            )

    preview_rows = min(4, max(2, (box_h - 8) // 3))
    list_rows = max(3, box_h - 6 - preview_rows)
    list_top = box_y + 3
    preview_top = list_top + list_rows
    hint_y = box_y + box_h - 2

    entry_count = len(entries)
    selected = min(max(0, selected), max(0, entry_count - 1))
    if entry_count <= list_rows:
        start = 0
    else:
        start = min(
            max(0, selected - (list_rows // 2)),
            max(0, entry_count - list_rows),
        )
    visible = entries[start : start + list_rows]

    hitboxes: list[HitBox] = []
    row_w = box_w - 4
    snippet_w = max(8, row_w - 8)
    row_idx_w = max(2, len(str(entry_count)))
    for offset, entry in enumerate(visible):
        row_y = list_top + offset
        row_index = start + offset
        is_selected = row_index == selected
        row_bg = theme.accent if is_selected else theme.bg_input
        num_fg = theme.bg if is_selected else theme.accent_warm
        row_fg = theme.bg if is_selected else theme.fg
        fill_region(
            grid,
            box_x + 1,
            row_y,
            box_w - 2,
            1,
            style=Style(bg=row_bg),
        )
        num_text = str(row_index + 1).rjust(row_idx_w)
        paint_text(
            grid,
            num_text,
            box_x + 2,
            row_y,
            style=Style(fg=fade(num_fg), bg=row_bg, attrs=ATTR_BOLD),
        )
        paint_text(
            grid,
            _history_snippet(entry, snippet_w),
            box_x + 4 + row_idx_w,
            row_y,
            style=Style(fg=fade(row_fg), bg=row_bg),
        )
        hitboxes.append(
            HitBox(box_x + 1, row_y, box_w - 2, 1, f"history:{row_index}")
        )

    if not visible:
        empty = "no matching history entries"
        ex = box_x + (box_w - len(empty)) // 2
        ey = list_top + max(0, list_rows // 2)
        paint_text(
            grid,
            empty,
            ex,
            ey,
            style=Style(
                fg=fade(theme.fg_dim),
                bg=theme.bg_input,
                attrs=ATTR_DIM,
            ),
        )

    if preview_top < hint_y:
        fill_region(
            grid,
            box_x + 1,
            preview_top,
            box_w - 2,
            1,
            style=Style(bg=theme.bg_footer),
        )
        paint_text(
            grid,
            " preview ",
            box_x + 2,
            preview_top,
            style=Style(
                fg=fade(theme.fg_dim),
                bg=theme.bg_footer,
                attrs=ATTR_BOLD,
            ),
        )

        preview_text = entries[selected] if entry_count else ""
        wrapped = hard_wrap(preview_text or " ", max(8, box_w - 4))
        preview_lines = wrapped[:preview_rows]
        for idx in range(preview_rows):
            row_y = preview_top + 1 + idx
            if row_y >= hint_y:
                break
            line = preview_lines[idx] if idx < len(preview_lines) else ""
            paint_text(
                grid,
                line,
                box_x + 2,
                row_y,
                style=Style(fg=fade(theme.fg), bg=theme.bg_input),
            )

    hint = "↑/↓ move · type to filter · enter load · esc close"
    hx = box_x + (box_w - len(hint)) // 2
    paint_text(
        grid,
        hint,
        hx,
        hint_y,
        style=Style(
            fg=fade(theme.fg_subtle),
            bg=theme.bg_input,
            attrs=ATTR_DIM,
        ),
    )
    return hitboxes
