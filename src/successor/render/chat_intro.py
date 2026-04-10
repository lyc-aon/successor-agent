"""Empty-state intro painter for the chat scene."""

from __future__ import annotations

import math
import time
from typing import Callable

from .braille import fit_dimensions
from .cells import ATTR_BOLD, ATTR_DIM, Grid, Style
from .paint import paint_box, paint_text
from .text import lerp_rgb
from .theme import ThemeVariant


def paint_empty_state(
    grid: Grid,
    top: int,
    bottom: int,
    width: int,
    theme: ThemeVariant,
    *,
    panel_lines: list[tuple[str, str, bool, bool]],
    resolve_intro_art: Callable[[], object | None],
) -> None:
    """Paint the empty-state hero portrait and right-hand info panel."""
    chat_h = bottom - top
    if chat_h < 6:
        return

    panel_h = len(panel_lines)

    panel_intrinsic_w = 0
    for label, value, is_header, is_hint in panel_lines:
        if is_hint:
            panel_intrinsic_w = max(panel_intrinsic_w, len(label))
        elif is_header:
            panel_intrinsic_w = max(panel_intrinsic_w, len(label))
        elif value:
            panel_intrinsic_w = max(panel_intrinsic_w, 2 + len(value))
    panel_intrinsic_w = max(26, panel_intrinsic_w)

    art = resolve_intro_art()
    outer_pad = 3 if width < 96 else (5 if width < 120 else 6)
    inner_gap = 3 if width < 100 else 6
    panel_w = min(max(panel_intrinsic_w + 2, 28), max(28, width - 2 * outer_pad))
    panel_x = max(outer_pad, width - outer_pad - panel_w)

    show_art = False
    art_lines: list[str] = []
    art_x = 0
    art_y = 0
    if art is not None and chat_h >= 12:
        art_box_x = outer_pad
        art_box_w = max(0, panel_x - inner_gap - art_box_x)
        art_avail_h = max(10, chat_h - (2 if width < 96 else 4))
        art_fit_w, art_fit_h = fit_dimensions(
            art.dot_h,
            art.dot_w,
            avail_cells_h=art_avail_h,
            avail_cells_w=art_box_w,
            pad_cells=0,
        )
        show_art = art_fit_w >= 14 and art_fit_h >= 10
        if show_art:
            try:
                art_lines = art.layout(art_fit_w, art_fit_h)
            except Exception:  # noqa: BLE001
                art_lines = []
            art_left_pad = 2 if width < 96 else 5
            art_x = art_box_x + max(0, min(art_left_pad, art_box_w - art_fit_w))
            art_y = top + max(0, (chat_h - len(art_lines)) // 2)

    right_anchor_panel = show_art
    if not show_art:
        panel_w = min(60, max(30, width - 8))
        panel_x = max(2, (width - panel_w) // 2)

    panel_y_start = top + max(0, (chat_h - panel_h) // 2)

    if show_art and art_lines:
        shell_left = max(outer_pad - 1, art_x - 3)
        shell_right = min(width - outer_pad + 1, panel_x + panel_w + 1)
        shell_top = max(top + 1, min(art_y, panel_y_start) - 1)
        shell_bottom = min(
            bottom - 2,
            max(art_y + len(art_lines) - 1, panel_y_start + panel_h - 1) + 1,
        )
        shell_w = shell_right - shell_left + 1
        shell_h = shell_bottom - shell_top + 1
        if shell_w >= 12 and shell_h >= 8:
            shell_border = Style(
                fg=lerp_rgb(theme.fg_subtle, theme.accent_warm, 0.20),
                bg=theme.bg,
                attrs=ATTR_DIM,
            )
            shell_fill = Style(
                fg=theme.fg,
                bg=lerp_rgb(theme.bg, theme.bg_input, 0.72),
                attrs=0,
            )
            paint_box(
                grid,
                shell_left,
                shell_top,
                shell_w,
                shell_h,
                style=shell_border,
                fill_style=shell_fill,
                chars=("╭", "╮", "╰", "╯", "┈", "┆"),
            )

        art_style = Style(
            fg=theme.accent,
            bg=lerp_rgb(theme.bg, theme.bg_input, 0.72),
            attrs=ATTR_BOLD,
        )
        for i, line in enumerate(art_lines):
            ly = art_y + i
            if ly >= bottom:
                break
            paint_text(grid, line, art_x, ly, style=art_style)
        paint_empty_state_oracle_fx(
            grid,
            lines=art_lines,
            art_x=art_x,
            art_y=art_y,
            theme=theme,
        )

    for i, (label, value, is_header, is_hint) in enumerate(panel_lines):
        ly = panel_y_start + i
        if ly >= bottom or ly < top:
            continue
        panel_bg = (
            lerp_rgb(theme.bg, theme.bg_input, 0.72) if right_anchor_panel else theme.bg
        )
        if is_hint:
            if right_anchor_panel:
                hint_x = panel_x + max(0, panel_w - len(label))
            else:
                hint_x = panel_x + max(0, (panel_w - len(label)) // 2)
            paint_text(
                grid,
                label,
                hint_x,
                ly,
                style=Style(
                    fg=theme.accent_warm,
                    bg=panel_bg,
                    attrs=ATTR_BOLD,
                ),
            )
            continue
        if is_header:
            header_x = panel_x
            if right_anchor_panel:
                header_x = panel_x + max(0, panel_w - len(label))
            paint_text(
                grid,
                label,
                header_x,
                ly,
                style=Style(
                    fg=lerp_rgb(theme.fg_dim, theme.accent_warm, 0.22),
                    bg=panel_bg,
                    attrs=ATTR_DIM | ATTR_BOLD,
                ),
            )
            continue
        value_text = value if right_anchor_panel else "  " + value
        value_x = panel_x
        if right_anchor_panel:
            value_x = panel_x + max(0, panel_w - len(value_text))
        paint_text(
            grid,
            value_text,
            value_x,
            ly,
            style=Style(
                fg=theme.fg,
                bg=panel_bg,
            ),
        )


def paint_empty_state_oracle_fx(
    grid: Grid,
    *,
    lines: list[str],
    art_x: int,
    art_y: int,
    theme: ThemeVariant,
) -> None:
    """Subtle idle motion for the empty-state oracle."""
    if not lines:
        return

    art_h = len(lines)
    art_w = max((len(line) for line in lines), default=0)
    if art_w <= 0 or art_h <= 0:
        return

    now = time.monotonic()

    crown_scan_t = (now / 8.0) % 1.0
    crown_band_y = 0.04 + 0.26 * crown_scan_t
    crown_style = Style(
        fg=lerp_rgb(theme.accent, theme.accent_warm, 0.62),
        bg=lerp_rgb(theme.bg, theme.bg_input, 0.72),
        attrs=ATTR_BOLD,
    )

    pulse = 0.5 + 0.5 * math.sin(now * 1.15)
    pulse_style = Style(
        fg=lerp_rgb(theme.accent, theme.fg, 0.18 + 0.28 * pulse),
        bg=lerp_rgb(theme.bg, theme.bg_input, 0.72),
        attrs=ATTR_BOLD,
    )

    for dy, line in enumerate(lines):
        if not line:
            continue
        ny = dy / max(1, art_h - 1)
        for dx, ch in enumerate(line):
            if ch in {" ", "⠀"}:
                continue
            nx = dx / max(1, art_w - 1)

            crown_band = crown_band_y + (nx - 0.5) * 0.08
            if ny <= 0.34 and abs(ny - crown_band) <= 0.035:
                paint_text(
                    grid,
                    ch,
                    art_x + dx,
                    art_y + dy,
                    style=crown_style,
                )
                continue

            core_shape = abs(nx - 0.5) * 1.65 + abs(ny - 0.67) * 1.05
            if pulse > 0.56 and 0.42 <= ny <= 0.92 and core_shape <= 0.27:
                paint_text(
                    grid,
                    ch,
                    art_x + dx,
                    art_y + dy,
                    style=pulse_style,
                )
