from __future__ import annotations

from types import SimpleNamespace

from successor.render.chat_viewport import compute_viewport_decision


def test_viewport_centers_capped_content_column() -> None:
    density = SimpleNamespace(gutter=4, max_content_width=80)
    decision = compute_viewport_decision(
        width=140,
        top=1,
        bottom=31,
        density=density,
        committed_height=10,
        scroll_offset=0,
        auto_scroll=True,
        last_total_height=10,
    )
    assert decision.body_width == 80
    assert decision.body_x == 30
    assert decision.chat_height == 30


def test_viewport_advances_scroll_anchor_when_content_grows() -> None:
    density = SimpleNamespace(gutter=1, max_content_width=120)
    decision = compute_viewport_decision(
        width=100,
        top=1,
        bottom=21,
        density=density,
        committed_height=60,
        scroll_offset=5,
        auto_scroll=False,
        last_total_height=50,
    )
    assert decision.scroll_offset == 15
    assert decision.auto_scroll is False


def test_viewport_clamps_scroll_offset_and_restores_auto_scroll_at_zero() -> None:
    density = SimpleNamespace(gutter=0, max_content_width=99999)
    decision = compute_viewport_decision(
        width=80,
        top=1,
        bottom=21,
        density=density,
        committed_height=8,
        scroll_offset=999,
        auto_scroll=False,
        last_total_height=8,
    )
    assert decision.scroll_offset == 0
    assert decision.auto_scroll is True
    assert decision.start == 0
    assert decision.end == 8
