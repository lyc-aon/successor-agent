"""Input and search-bar painters for the chat scene."""

from __future__ import annotations

import time

from .cells import ATTR_BOLD, ATTR_DIM, ATTR_ITALIC, Cell, Grid, Style
from .paint import fill_region, paint_text
from .theme import ThemeVariant


def paint_input(
    grid: Grid,
    y: int,
    height: int,
    width: int,
    theme: ThemeVariant,
    *,
    wrapped_lines: list[str],
    hidden_above: int,
    prompt: str,
    prompt_width: int,
    cursor_blink_hz: float,
    ghost_text: str,
    stream_active: bool,
) -> None:
    fill_region(grid, 0, y, width, height, style=Style(bg=theme.bg_input))

    prompt_style = Style(fg=theme.accent, bg=theme.bg_input, attrs=ATTR_BOLD)
    paint_text(grid, prompt, 0, y, style=prompt_style)

    text_style = Style(fg=theme.fg, bg=theme.bg_input)
    for i, line in enumerate(wrapped_lines):
        ly = y + i
        if ly >= y + height:
            break
        paint_text(grid, line, prompt_width, ly, style=text_style)

    if hidden_above > 0:
        badge = f"↑ {hidden_above} more {'line' if hidden_above == 1 else 'lines'}"
        badge_x = max(prompt_width, width - len(badge) - 1)
        paint_text(
            grid,
            badge,
            badge_x,
            y,
            style=Style(
                fg=theme.accent_warm,
                bg=theme.bg_input,
                attrs=ATTR_DIM | ATTR_ITALIC,
            ),
        )

    if not stream_active:
        last_line = wrapped_lines[-1] if wrapped_lines else ""
        last_y = y + min(len(wrapped_lines) - 1, height - 1)
        cursor_x = min(width - 1, prompt_width + len(last_line))

        if ghost_text and cursor_x < width:
            ghost_x = cursor_x
            cursor_visible = (int(time.monotonic() * cursor_blink_hz * 2) % 2) == 0
            if cursor_visible:
                ghost_x += 1
            avail = max(0, width - ghost_x)
            if avail > 0:
                paint_text(
                    grid,
                    ghost_text[:avail],
                    ghost_x,
                    last_y,
                    style=Style(
                        fg=theme.fg_subtle,
                        bg=theme.bg_input,
                        attrs=ATTR_DIM | ATTR_ITALIC,
                    ),
                )

        visible = (int(time.monotonic() * cursor_blink_hz * 2) % 2) == 0
        if visible:
            cursor_cell = Cell(" ", Style(fg=theme.bg_input, bg=theme.fg))
            grid.set(last_y, cursor_x, cursor_cell)
    else:
        hint = "successor is responding…  Ctrl+G to interrupt"
        paint_text(
            grid,
            hint,
            prompt_width,
            y,
            style=Style(fg=theme.fg_dim, bg=theme.bg_input, attrs=ATTR_DIM),
        )


def paint_search_bar(
    grid: Grid,
    y: int,
    width: int,
    theme: ThemeVariant,
    *,
    query_text: str,
    match_count: int,
    focused_index: int,
    cursor_blink_hz: float,
) -> None:
    fill_region(grid, 0, y, width, 1, style=Style(bg=theme.bg_input))

    prompt = "search ▸ "
    prompt_style = Style(
        fg=theme.accent_warm,
        bg=theme.bg_input,
        attrs=ATTR_BOLD,
    )
    paint_text(grid, prompt, 0, y, style=prompt_style)

    query_style = Style(fg=theme.fg, bg=theme.bg_input)
    paint_text(grid, query_text, len(prompt), y, style=query_style)

    cursor_x = min(width - 1, len(prompt) + len(query_text))
    cursor_visible = (int(time.monotonic() * cursor_blink_hz * 2) % 2) == 0
    if cursor_visible:
        grid.set(
            y, cursor_x,
            Cell(" ", Style(fg=theme.bg_input, bg=theme.fg)),
        )

    if match_count:
        counter = f" {focused_index + 1}/{match_count} "
        counter_style = Style(
            fg=theme.bg,
            bg=theme.accent_warm,
            attrs=ATTR_BOLD,
        )
    else:
        if query_text:
            counter = " no matches "
        else:
            counter = " type to search "
        counter_style = Style(
            fg=theme.fg_dim,
            bg=theme.bg_input,
            attrs=ATTR_DIM,
        )

    hint = "  ↑↓ jump  Esc close"
    hint_style = Style(fg=theme.fg_subtle, bg=theme.bg_input, attrs=ATTR_DIM)

    right_text = counter + hint
    right_x = max(len(prompt) + len(query_text) + 2, width - len(right_text))
    counter_x = right_x
    if 0 <= counter_x < width:
        paint_text(grid, counter, counter_x, y, style=counter_style)
    hint_x = counter_x + len(counter)
    if 0 <= hint_x < width:
        paint_text(grid, hint, hint_x, y, style=hint_style)
