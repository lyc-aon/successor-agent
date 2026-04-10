from __future__ import annotations

from successor.render.chat_frame import compute_chat_frame
from successor.render.chat_header import build_header_plan
from successor.render.theme import get_theme


def test_compute_chat_frame_stacks_regions_bottom_up() -> None:
    layout = compute_chat_frame(rows=30, cols=100, input_h=3)
    assert layout.title_h == 1
    assert layout.footer_h == 1
    assert layout.static_y == 29
    assert layout.input_y == 26
    assert layout.chat_top == 1
    assert layout.chat_bottom == 26


def test_header_plan_exposes_expected_actions() -> None:
    theme = get_theme("steel").dark
    plan = build_header_plan(
        cols=120,
        theme=theme,
        theme_name="steel",
        theme_icon="◆",
        display_mode="dark",
        density_name="normal",
        profile_name="default",
        task_total=3,
        task_active=1,
        scroll_offset=4,
        max_scroll=10,
        stream_active=False,
    )
    actions = [hitbox.action for hitbox in plan.hitboxes]
    assert actions == [
        "theme",
        "mode",
        "density",
        "profile",
        "tasks",
        "scroll_to_bottom",
    ]


def test_header_plan_clamps_title_left_when_controls_expand() -> None:
    theme = get_theme("paper").light
    plan = build_header_plan(
        cols=60,
        theme=theme,
        theme_name="paper",
        theme_icon="◌",
        display_mode="light",
        density_name="spacious",
        profile_name="michaelreal",
        task_total=8,
        task_active=8,
        scroll_offset=0,
        max_scroll=0,
        stream_active=False,
    )
    title = next(item for item in plan.placements if item.text == " successor · chat ")
    assert title.x == 2
