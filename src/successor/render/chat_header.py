"""Header composition for the chat scene."""

from __future__ import annotations

from .cells import ATTR_BOLD, ATTR_DIM, Style
from .chat_frame import HeaderPlan, HitBox, PlacedText
from .theme import ThemeVariant


def build_header_plan(
    *,
    cols: int,
    theme: ThemeVariant,
    theme_name: str,
    theme_icon: str,
    display_mode: str,
    density_name: str,
    profile_name: str,
    task_total: int,
    task_active: int,
    scroll_offset: int,
    max_scroll: int,
    stream_active: bool,
    title: str = " successor · chat ",
) -> HeaderPlan:
    placements: list[PlacedText] = []
    hitboxes: list[HitBox] = []

    theme_label = f" {theme_icon} {theme_name} "
    theme_style = Style(
        fg=theme.bg,
        bg=theme.accent,
        attrs=ATTR_BOLD,
    )
    theme_x = max(0, cols - len(theme_label))
    placements.append(PlacedText(theme_label, theme_x, 0, theme_style, "theme"))
    hitboxes.append(HitBox(theme_x, 0, len(theme_label), 1, "theme"))

    mode_icon = "\u263e" if display_mode == "dark" else "\u2600"
    mode_label = f" {mode_icon} "
    mode_style = Style(
        fg=theme.bg,
        bg=theme.fg_dim,
        attrs=ATTR_BOLD,
    )
    mode_x = max(0, theme_x - len(mode_label) - 1)
    placements.append(PlacedText(mode_label, mode_x, 0, mode_style, "mode"))
    hitboxes.append(HitBox(mode_x, 0, len(mode_label), 1, "mode"))

    density_label = f" {density_name} "
    density_style = Style(
        fg=theme.bg,
        bg=theme.accent_warm,
        attrs=ATTR_BOLD,
    )
    density_x = max(0, mode_x - len(density_label) - 1)
    placements.append(
        PlacedText(density_label, density_x, 0, density_style, "density")
    )
    hitboxes.append(HitBox(density_x, 0, len(density_label), 1, "density"))

    profile_label = f" {profile_name} "
    profile_style = Style(
        fg=theme.fg_dim,
        bg=theme.bg,
        attrs=ATTR_DIM | ATTR_BOLD,
    )
    profile_x = max(0, density_x - len(profile_label) - 1)
    placements.append(
        PlacedText(profile_label, profile_x, 0, profile_style, "profile")
    )
    hitboxes.append(HitBox(profile_x, 0, len(profile_label), 1, "profile"))

    task_anchor_x = profile_x
    if task_total > 0:
        if task_active > 0:
            task_label = f" tasks {task_active}/{task_total} "
            task_style = Style(
                fg=theme.bg,
                bg=theme.accent_warm,
                attrs=ATTR_BOLD,
            )
        else:
            task_label = f" tasks {task_total} "
            task_style = Style(
                fg=theme.bg,
                bg=theme.fg_dim,
                attrs=ATTR_BOLD,
            )
        task_x = max(0, profile_x - len(task_label) - 1)
        placements.append(PlacedText(task_label, task_x, 0, task_style, "tasks"))
        hitboxes.append(HitBox(task_x, 0, len(task_label), 1, "tasks"))
        task_anchor_x = task_x

    title_style = Style(fg=theme.fg, bg=theme.bg, attrs=ATTR_BOLD)
    desired_tx = max(2, (cols - len(title)) // 2)
    max_tx = max(2, task_anchor_x - len(title) - 3)
    tx = min(desired_tx, max_tx)
    placements.append(PlacedText(title, tx, 0, title_style))

    if scroll_offset > 0:
        if stream_active:
            indicator = (
                f" ↑ {scroll_offset} · successor responding · Ctrl+E newest "
            )
        else:
            indicator = f" ↑ {scroll_offset}/{max_scroll} · End for newest "
        ix = max(0, task_anchor_x - len(indicator))
        indicator_style = Style(
            fg=theme.accent_warm,
            bg=theme.bg,
            attrs=ATTR_BOLD,
        )
        placements.append(
            PlacedText(indicator, ix, 0, indicator_style, "scroll_to_bottom")
        )
        hitboxes.append(HitBox(ix, 0, len(indicator), 1, "scroll_to_bottom"))

    return HeaderPlan(tuple(placements), tuple(hitboxes))
