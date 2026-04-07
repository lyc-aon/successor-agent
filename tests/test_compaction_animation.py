"""Tests for the compaction animation in SuccessorChat.

Three layers:
  1. _CompactionAnimation phase machine — pure logic, no rendering
  2. paint_horizontal_divider primitive — pure paint function
  3. Chat-level animation orchestration — drive an animation through
     each phase, verify the rendered grid contains the right artifacts
     at the right times

The chat-level tests use a deterministic time source (overriding
animation.started_at) so phase boundaries are exact.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from successor.agent.log import BoundaryMarker
from successor.chat import (
    SuccessorChat,
    _CompactionAnimation,
    _Message,
    _COMPACT_ANTICIPATION_S,
    _COMPACT_FOLD_S,
    _COMPACT_MATERIALIZE_S,
    _COMPACT_REVEAL_S,
)
from successor.render.cells import Grid, Style
from successor.render.paint import paint_horizontal_divider
from successor.snapshot import render_grid_to_plain


# ─── Phase machine ───


def _make_anim(now: float | None = None) -> _CompactionAnimation:
    boundary = BoundaryMarker(
        happened_at=now or time.monotonic(),
        pre_compact_tokens=10000, post_compact_tokens=500,
        rounds_summarized=20, summary_text="x", reason="manual",
    )
    return _CompactionAnimation(
        started_at=now or time.monotonic(),
        pre_compact_snapshot=[],
        pre_compact_count=0,
        boundary=boundary,
        summary_text="x",
        reason="manual",
    )


def test_phase_at_anticipation() -> None:
    anim = _make_anim(now=100.0)
    phase, t = anim.phase_at(100.0)
    assert phase == "anticipation"
    assert t == 0.0
    phase, t = anim.phase_at(100.0 + _COMPACT_ANTICIPATION_S * 0.5)
    assert phase == "anticipation"
    assert t == pytest.approx(0.5, abs=0.01)


def test_phase_at_fold() -> None:
    anim = _make_anim(now=100.0)
    fold_start = 100.0 + _COMPACT_ANTICIPATION_S
    phase, t = anim.phase_at(fold_start + _COMPACT_FOLD_S * 0.25)
    assert phase == "fold"
    assert t == pytest.approx(0.25, abs=0.01)


def test_phase_at_materialize() -> None:
    anim = _make_anim(now=100.0)
    materialize_start = 100.0 + _COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S
    phase, t = anim.phase_at(materialize_start + _COMPACT_MATERIALIZE_S * 0.5)
    assert phase == "materialize"
    assert t == pytest.approx(0.5, abs=0.01)


def test_phase_at_reveal() -> None:
    anim = _make_anim(now=100.0)
    reveal_start = (
        100.0 + _COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S + _COMPACT_MATERIALIZE_S
    )
    phase, t = anim.phase_at(reveal_start + _COMPACT_REVEAL_S * 0.5)
    assert phase == "reveal"
    assert t == pytest.approx(0.5, abs=0.01)


def test_phase_at_done() -> None:
    anim = _make_anim(now=100.0)
    # Way past everything
    phase, t = anim.phase_at(200.0)
    assert phase == "done"
    assert anim.is_done(200.0)


def test_phase_at_pending_clamps() -> None:
    """Clock running before started_at returns 'pending'."""
    anim = _make_anim(now=100.0)
    phase, t = anim.phase_at(99.0)
    assert phase == "pending"


# ─── paint_horizontal_divider primitive ───


def test_divider_full_width() -> None:
    g = Grid(3, 20)
    drawn = paint_horizontal_divider(g, 0, 1, 20, char="━", t=1.0)
    assert drawn == 20
    plain = render_grid_to_plain(g)
    assert "━" * 20 in plain


def test_divider_half_progress() -> None:
    g = Grid(3, 20)
    drawn = paint_horizontal_divider(g, 0, 1, 20, char="━", t=0.5)
    # Half of width 20 is 10 cells from center
    assert drawn > 0
    assert drawn < 20


def test_divider_zero_progress() -> None:
    g = Grid(3, 20)
    drawn = paint_horizontal_divider(g, 0, 1, 20, char="━", t=0.0)
    assert drawn == 0


def test_divider_grows_from_center() -> None:
    """The line should appear at the center first and grow outward."""
    g = Grid(3, 20)
    paint_horizontal_divider(g, 0, 1, 20, char="X", t=0.1)
    plain = render_grid_to_plain(g)
    line = plain.split("\n")[1]
    # Center of width 20 is column 10. The drawn cells should be near column 10.
    if "X" in line:
        first_x = line.index("X")
        last_x = line.rindex("X")
        center = (first_x + last_x) // 2
        # Center should be within ±2 of column 10
        assert 8 <= center <= 12


def test_divider_clamps_t_above_one() -> None:
    g = Grid(3, 20)
    drawn = paint_horizontal_divider(g, 0, 1, 20, char="━", t=2.0)
    # t > 1 should clamp to full width
    assert drawn == 20


def test_divider_skips_offscreen_y() -> None:
    g = Grid(3, 20)
    drawn = paint_horizontal_divider(g, 0, 5, 20, char="━", t=1.0)
    assert drawn == 0


# ─── Chat-level animation orchestration ───


def _build_chat_with_anim(
    elapsed: float = 0.0,
    *,
    snapshot_size: int = 4,
) -> tuple[SuccessorChat, _CompactionAnimation]:
    chat = SuccessorChat()
    chat.messages = []
    snapshot = []
    for i in range(snapshot_size):
        snapshot.append(_Message("user", f"q{i} synthetic"))
        snapshot.append(_Message("successor", f"a{i} response"))
    boundary = BoundaryMarker(
        happened_at=time.monotonic(),
        pre_compact_tokens=10000, post_compact_tokens=500,
        rounds_summarized=snapshot_size, summary_text="test summary",
        reason="manual",
    )
    chat.messages = [
        _Message("successor", "", is_boundary=True, boundary_meta=boundary),
        _Message("successor", "test summary", is_summary=True, boundary_meta=boundary),
    ]
    anim = _CompactionAnimation(
        started_at=time.monotonic() - elapsed,
        pre_compact_snapshot=snapshot,
        pre_compact_count=len(snapshot),
        boundary=boundary,
        summary_text="test summary",
        reason="manual",
    )
    chat._compaction_anim = anim
    return chat, anim


def test_anim_anticipation_paints_snapshot(temp_config_dir: Path) -> None:
    chat, _ = _build_chat_with_anim(elapsed=0.05)  # mid-anticipation
    g = Grid(30, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    # Snapshot content visible
    assert "q0 synthetic" in plain
    # Boundary NOT visible yet
    assert "rounds" not in plain or "saved" not in plain


def test_anim_fold_dims_snapshot(temp_config_dir: Path) -> None:
    """During fold, snapshot characters should be present in the grid
    but with foreground colors trending toward bg."""
    chat, _ = _build_chat_with_anim(elapsed=0.05)  # anticipation
    g_anticipation = Grid(30, 100)
    chat.on_tick(g_anticipation)

    chat, _ = _build_chat_with_anim(elapsed=0.9)  # mid-fold
    g_mid_fold = Grid(30, 100)
    chat.on_tick(g_mid_fold)

    chat, _ = _build_chat_with_anim(elapsed=1.45)  # near end of fold
    g_end_fold = Grid(30, 100)
    chat.on_tick(g_end_fold)

    # Find a content cell ("q") in each grid and check the fg → bg distance
    theme = chat._current_variant()
    bg = theme.bg

    def _content_cell_fg(g: Grid) -> int | None:
        for ry in range(g.rows):
            for cx in range(g.cols):
                cell = g.at(ry, cx)
                if cell.char == "q":
                    return cell.style.fg
        return None

    fg_anticipation = _content_cell_fg(g_anticipation)
    fg_mid = _content_cell_fg(g_mid_fold)
    fg_end = _content_cell_fg(g_end_fold)

    assert fg_anticipation is not None
    assert fg_mid is not None
    assert fg_end is not None

    def _dist(c1: int, c2: int) -> int:
        return (
            abs((c1 >> 16 & 0xFF) - (c2 >> 16 & 0xFF))
            + abs((c1 >> 8 & 0xFF) - (c2 >> 8 & 0xFF))
            + abs((c1 & 0xFF) - (c2 & 0xFF))
        )

    d_anticipation = _dist(fg_anticipation, bg)
    d_mid = _dist(fg_mid, bg)
    d_end = _dist(fg_end, bg)

    # Strict ordering: each later phase should be closer to bg
    assert d_anticipation > d_mid, "anticipation should be more visible than fold mid"
    assert d_mid > d_end, "fold mid should be more visible than fold end"


def test_anim_materialize_shows_growing_divider(temp_config_dir: Path) -> None:
    """During materialize, the boundary divider character (━) should
    appear with progressively more cells."""
    counts: list[int] = []
    for elapsed in [1.55, 1.70, 1.85]:
        chat, _ = _build_chat_with_anim(elapsed=elapsed)
        g = Grid(30, 100)
        chat.on_tick(g)
        # Count ━ characters in the grid
        n = sum(
            1 for ry in range(g.rows) for cx in range(g.cols)
            if g.at(ry, cx).char == "━"
        )
        counts.append(n)
    # Each later frame should have more divider chars (or equal — ease curve
    # may plateau briefly near the end)
    assert counts[0] <= counts[1] <= counts[2]
    assert counts[2] > counts[0]  # but at least some growth


def test_anim_reveal_shows_summary_pill(temp_config_dir: Path) -> None:
    """During reveal phase, the boundary pill is fully visible."""
    chat, _ = _build_chat_with_anim(elapsed=2.0)  # in reveal
    g = Grid(30, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    # The pill text should be visible
    assert "rounds" in plain or "saved" in plain or "▼" in plain


def test_anim_done_clears_state(temp_config_dir: Path) -> None:
    """After the animation duration elapses, _compaction_anim is cleared."""
    chat, _ = _build_chat_with_anim(elapsed=10.0)  # way past done
    assert chat._compaction_anim is not None
    g = Grid(30, 100)
    chat.on_tick(g)  # one tick should clear it
    assert chat._compaction_anim is None


def test_post_anim_boundary_still_renders(temp_config_dir: Path) -> None:
    """After the animation completes, the boundary message stays in the
    log and renders as a permanent visible divider."""
    chat, _ = _build_chat_with_anim(elapsed=10.0)
    g = Grid(30, 100)
    chat.on_tick(g)
    assert chat._compaction_anim is None
    # Re-render — boundary should still be there
    g2 = Grid(30, 100)
    chat.on_tick(g2)
    plain = render_grid_to_plain(g2)
    assert "━" in plain
    assert "rounds" in plain or "saved" in plain


# ─── Boundary marker rendering (post-animation steady state) ───


def test_boundary_message_in_chat_renders_divider(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    boundary = BoundaryMarker(
        happened_at=time.monotonic(),
        pre_compact_tokens=20000, post_compact_tokens=800,
        rounds_summarized=15, summary_text="x", reason="manual",
    )
    chat.messages.append(_Message("user", "before"))
    chat.messages.append(_Message(
        "successor", "", is_boundary=True, boundary_meta=boundary,
    ))
    chat.messages.append(_Message(
        "successor", "summary content here",
        is_summary=True, boundary_meta=boundary,
    ))
    chat.messages.append(_Message("user", "after"))
    g = Grid(30, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    assert "before" in plain
    assert "after" in plain
    assert "━" in plain  # divider line
    assert "15 rounds" in plain  # pill content
