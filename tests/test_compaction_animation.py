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
from dataclasses import dataclass, field
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


def _make_anim(
    now: float | None = None,
    *,
    result_arrived_at: float | None = None,
) -> _CompactionAnimation:
    """Build a test animation. By default the result is "arrived
    immediately at started_at" so the materialize/reveal/done phases
    play on the old fixed schedule for backward-compat with existing
    tests. Pass result_arrived_at=None to leave the animation in the
    waiting phase."""
    n = now or time.monotonic()
    boundary = BoundaryMarker(
        happened_at=n,
        pre_compact_tokens=10000, post_compact_tokens=500,
        rounds_summarized=20, summary_text="x", reason="manual",
    )
    # By default the result already arrived at started_at + fold_end
    # so the post-fold phases play on a fixed schedule (matches the
    # old animation timing).
    if result_arrived_at is None:
        from successor.chat import _COMPACT_ANTICIPATION_S, _COMPACT_FOLD_S
        result_arrived_at = n + _COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S
    return _CompactionAnimation(
        started_at=n,
        pre_compact_snapshot=[],
        pre_compact_count=0,
        boundary=boundary,
        summary_text="x",
        reason="manual",
        result_arrived_at=result_arrived_at,
    )


def _make_waiting_anim(now: float | None = None) -> _CompactionAnimation:
    """Build a test animation in the waiting phase (no result yet)."""
    n = now or time.monotonic()
    return _CompactionAnimation(
        started_at=n,
        pre_compact_snapshot=[],
        pre_compact_count=0,
        boundary=None,
        summary_text="",
        reason="manual",
        result_arrived_at=None,
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


def test_phase_at_waiting_when_no_result_yet() -> None:
    """After fold completes, if result_arrived_at is None, the
    phase should be 'waiting' indefinitely."""
    anim = _make_waiting_anim(now=100.0)
    fold_end = 100.0 + _COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S
    # Just past fold end
    phase, t = anim.phase_at(fold_end + 0.5)
    assert phase == "waiting"
    assert t == pytest.approx(0.5, abs=0.01)
    # Way past fold end — still waiting
    phase, t = anim.phase_at(fold_end + 60.0)
    assert phase == "waiting"
    assert t == pytest.approx(60.0, abs=0.01)


def test_phase_at_waiting_transitions_to_materialize_on_result() -> None:
    """Setting result_arrived_at transitions out of waiting."""
    anim = _make_waiting_anim(now=100.0)
    fold_end = 100.0 + _COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S
    # In waiting at fold_end + 5s
    waiting_at = fold_end + 5.0
    phase, t = anim.phase_at(waiting_at)
    assert phase == "waiting"
    # Set result_arrived_at to "now" (fold_end + 5)
    # Mutate the dataclass (slots=True, not frozen)
    object.__setattr__(anim, "result_arrived_at", waiting_at)
    # Materialize should start
    phase, t = anim.phase_at(waiting_at + 0.1)
    assert phase == "materialize"
    assert t == pytest.approx(0.25, abs=0.05)


def test_spinner_frame_animates() -> None:
    anim = _make_waiting_anim(now=0.0)
    frames = set()
    for tenth_sec in range(20):  # 2 seconds of frames at 10 Hz
        frames.add(anim.spinner_frame(tenth_sec * 0.1))
    # Should cycle through several distinct frames
    assert len(frames) >= 5


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
    # The result_arrived_at anchor places materialize/reveal/toast on
    # the same fixed schedule as the old timing model, so existing
    # elapsed-based tests work without changes. Set it to fold_end
    # so phases play immediately after fold completes.
    from successor.chat import _COMPACT_ANTICIPATION_S, _COMPACT_FOLD_S
    started = time.monotonic() - elapsed
    fold_end = started + _COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S
    anim = _CompactionAnimation(
        started_at=started,
        pre_compact_snapshot=snapshot,
        pre_compact_count=len(snapshot),
        boundary=boundary,
        summary_text="test summary",
        reason="manual",
        result_arrived_at=fold_end,  # immediate transition after fold
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


def test_waiting_overlay_shows_spinner_and_status(temp_config_dir: Path) -> None:
    """During the WAITING phase, the chat should show a centered
    spinner + 'compacting N rounds' status indicator."""
    chat = SuccessorChat()
    chat.messages = []
    snapshot = []
    for i in range(4):
        snapshot.append(_Message("user", f"q{i}"))
        snapshot.append(_Message("successor", f"a{i}"))

    # Arm the animation in waiting state — no result yet
    from successor.chat import (
        _COMPACT_ANTICIPATION_S, _COMPACT_FOLD_S,
    )
    started = time.monotonic() - (_COMPACT_ANTICIPATION_S + _COMPACT_FOLD_S + 1.0)
    chat._compaction_anim = _CompactionAnimation(
        started_at=started,
        pre_compact_snapshot=snapshot,
        pre_compact_count=len(snapshot),
        boundary=None,  # not arrived yet
        summary_text="",
        reason="manual",
        result_arrived_at=None,  # still waiting
        pre_compact_tokens=12345,
        rounds_summarized=42,
    )
    g = Grid(30, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    assert "compacting" in plain
    assert "42 rounds" in plain
    assert "12,345 tokens" in plain
    assert "elapsed" in plain
    assert "Ctrl+G" in plain


# ─── Worker thread integration ───


class _MockCompactClient:
    """Fake CompactionClient that simulates a slow compact response."""
    def __init__(self, summary_text: str = "mock summary text", delay_s: float = 0.0):
        self.summary_text = summary_text
        self.delay_s = delay_s
        self.base_url = "http://mock"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None):
        from successor.providers.llama import (
            ContentChunk, StreamEnded, StreamStarted,
        )
        if self.delay_s:
            time.sleep(self.delay_s)
        return _MockStream(events=[
            StreamStarted(),
            ContentChunk(text=self.summary_text),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ])


@dataclass
class _MockStream:
    events: list = field(default_factory=list)
    _drained: bool = False

    def drain(self):
        if self._drained:
            return []
        self._drained = True
        return list(self.events)

    def close(self):
        pass


def test_worker_runs_in_background(temp_config_dir: Path) -> None:
    """The worker thread should not block the main thread."""
    from successor.chat import _CompactionWorker
    from successor.agent import MessageLog, LogMessage, TokenCounter

    client = _MockCompactClient(delay_s=0.1)
    counter = TokenCounter()
    log = MessageLog(system_prompt="sys")
    for i in range(10):
        log.begin_round()
        log.append_to_current_round(LogMessage(role="user", content=f"q{i}"))
        log.append_to_current_round(LogMessage(role="assistant", content=f"a{i}"))

    worker = _CompactionWorker(log=log, client=client, counter=counter)
    t0 = time.monotonic()
    worker.start()
    # Worker is async — start() returns immediately
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"start() took {elapsed:.3f}s, should be near zero"

    # Poll until done
    deadline = time.monotonic() + 5.0
    while worker.is_running() and time.monotonic() < deadline:
        time.sleep(0.05)

    result = worker.poll()
    assert result is not None
    assert result.error is None
    assert result.boundary is not None
    assert "mock summary" in result.boundary.summary_text


def test_worker_close_aborts_pending_result(temp_config_dir: Path) -> None:
    """close() before completion discards the result."""
    from successor.chat import _CompactionWorker
    from successor.agent import MessageLog, LogMessage, TokenCounter

    client = _MockCompactClient(delay_s=0.5)  # slow enough to abort
    counter = TokenCounter()
    log = MessageLog(system_prompt="sys")
    for i in range(10):
        log.begin_round()
        log.append_to_current_round(LogMessage(role="user", content=f"q{i}"))
        log.append_to_current_round(LogMessage(role="assistant", content=f"a{i}"))

    worker = _CompactionWorker(log=log, client=client, counter=counter)
    worker.start()
    time.sleep(0.05)  # let it start running
    worker.close()
    # Wait a bit for the worker's HTTP call to complete
    time.sleep(0.6)
    # Result should NOT have been recorded (close() set the stop flag
    # before the worker stored its result)
    result = worker.poll()
    assert result is None


def test_handle_compact_cmd_is_non_blocking(temp_config_dir: Path) -> None:
    """/compact should return immediately, not wait for the worker."""
    from successor.agent import TokenCounter
    chat = SuccessorChat()
    chat.client = _MockCompactClient(delay_s=0.3)
    # Inject a heuristic-only counter so the test doesn't hit the
    # mock client's bogus /tokenize URL
    chat._cached_token_counter = TokenCounter()
    chat.messages = []
    # Need at least 4 rounds for compaction not to refuse
    for i in range(6):
        chat.messages.append(_Message("user", f"q{i}"))
        chat.messages.append(_Message("successor", f"a{i}"))

    chat.input_buffer = "/compact"
    t0 = time.monotonic()
    chat._submit()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2, f"/compact took {elapsed:.3f}s, should be near zero"
    # Animation armed
    assert chat._compaction_anim is not None
    # Worker spawned
    assert chat._compaction_worker is not None

    # Wait for worker to complete
    deadline = time.monotonic() + 5.0
    while chat._compaction_worker is not None and time.monotonic() < deadline:
        chat._poll_compaction_worker()
        time.sleep(0.05)

    # After polling, the worker should be cleared and the animation
    # should have a result_arrived_at
    assert chat._compaction_worker is None
    assert chat._compaction_anim is not None
    assert chat._compaction_anim.result_arrived_at is not None


# ─── Cache warmer ───


class _SlowWarmingClient:
    """Mock client whose stream_chat takes a controlled delay before
    returning. Used to test warmer cancellation."""
    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s
        self.call_count = 0
        self.last_max_tokens = None
        self.base_url = "http://mock"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None):
        from successor.providers.llama import (
            ContentChunk, StreamEnded, StreamStarted,
        )
        self.call_count += 1
        self.last_max_tokens = max_tokens
        if self.delay_s:
            time.sleep(self.delay_s)
        return _MockStream(events=[
            StreamStarted(),
            ContentChunk(text="ok"),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ])


def test_warmer_starts_returns_immediately(temp_config_dir: Path) -> None:
    from successor.chat import _CacheWarmer
    client = _SlowWarmingClient(delay_s=0.5)
    warmer = _CacheWarmer(messages=[{"role": "user", "content": "x"}], client=client)
    t0 = time.monotonic()
    warmer.start()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"start() took {elapsed:.3f}s"
    # Wait for completion
    deadline = time.monotonic() + 5.0
    while warmer.is_running() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert warmer.is_done()


def test_warmer_uses_max_tokens_one(temp_config_dir: Path) -> None:
    """The warmer must use max_tokens=1 to keep generation cost minimal."""
    from successor.chat import _CacheWarmer
    client = _SlowWarmingClient()
    warmer = _CacheWarmer(messages=[{"role": "user", "content": "x"}], client=client)
    warmer.start()
    deadline = time.monotonic() + 5.0
    while warmer.is_running() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.last_max_tokens == 1


def test_warmer_close_aborts_in_flight(temp_config_dir: Path) -> None:
    """close() before completion should set the stop event and the
    warmer should exit promptly without storing a result."""
    from successor.chat import _CacheWarmer
    client = _SlowWarmingClient(delay_s=0.5)
    warmer = _CacheWarmer(messages=[{"role": "user", "content": "x"}], client=client)
    warmer.start()
    time.sleep(0.05)  # let it start running
    warmer.close()
    # Should be marked done quickly (worker thread sees stop event)
    deadline = time.monotonic() + 2.0
    while not warmer.is_done() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert warmer.is_done()


def test_warmer_silent_failure_on_exception(temp_config_dir: Path) -> None:
    """Warming is best-effort — exceptions should not propagate."""
    from successor.chat import _CacheWarmer

    class _BrokenClient:
        base_url = "http://mock"
        def stream_chat(self, *args, **kwargs):
            raise RuntimeError("intentional break")

    warmer = _CacheWarmer(
        messages=[{"role": "user", "content": "x"}],
        client=_BrokenClient(),
    )
    warmer.start()
    deadline = time.monotonic() + 2.0
    while not warmer.is_done() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert warmer.is_done()
    # No exception was raised — warming silently failed


def test_warmer_spawned_after_compaction(temp_config_dir: Path) -> None:
    """When _poll_compaction_worker applies a successful result, it
    should spawn a _CacheWarmer for the post-compact prefix."""
    from successor.agent import TokenCounter
    chat = SuccessorChat()
    chat.client = _MockCompactClient(delay_s=0.05)
    chat._cached_token_counter = TokenCounter()
    chat.messages = []
    for i in range(8):
        chat.messages.append(_Message("user", f"q{i}"))
        chat.messages.append(_Message("successor", f"a{i}"))

    chat.input_buffer = "/compact"
    chat._submit()

    # Tick until worker completes
    deadline = time.monotonic() + 5.0
    while chat._compaction_worker is not None and time.monotonic() < deadline:
        chat._poll_compaction_worker()
        time.sleep(0.05)

    # Warmer should have been spawned
    assert chat._cache_warmer is not None
    # Wait for warmer to finish
    deadline = time.monotonic() + 5.0
    while chat._cache_warmer is not None and not chat._cache_warmer.is_done() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert chat._cache_warmer is not None  # not yet cleared by on_tick
    assert chat._cache_warmer.is_done()


def test_submit_cancels_in_flight_warmer(temp_config_dir: Path) -> None:
    """_submit must cancel any in-flight cache warmer because the
    user's message takes priority."""
    from successor.chat import _CacheWarmer
    chat = SuccessorChat()
    chat.client = _SlowWarmingClient(delay_s=2.0)  # slow enough to abort
    chat.messages = []

    # Manually arm a warmer (simulating post-compaction state)
    chat._cache_warmer = _CacheWarmer(
        messages=[{"role": "user", "content": "x"}],
        client=chat.client,
    )
    chat._cache_warmer.start()
    time.sleep(0.05)  # let it start
    assert chat._cache_warmer.is_running()

    # User sends a message
    chat.input_buffer = "hello"
    # Mock the stream so _submit doesn't try to actually call the model
    # We just need to verify the warmer was canceled
    saved_warmer = chat._cache_warmer
    try:
        chat._submit()
    except Exception:
        pass  # we don't care about the rest of _submit's behavior

    # The warmer should be canceled (close() was called)
    assert chat._cache_warmer is None
    # The original warmer should be marked done after the close
    deadline = time.monotonic() + 1.0
    while not saved_warmer.is_done() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert saved_warmer.is_done()


def test_footer_warming_indicator(temp_config_dir: Path) -> None:
    """When the warmer is running, the static footer shows a small
    'warming' indicator next to the context bar."""
    from successor.chat import _CacheWarmer
    from successor.agent import TokenCounter
    chat = SuccessorChat()
    chat.messages = []
    chat._cached_token_counter = TokenCounter()
    chat._cache_warmer = _CacheWarmer(
        messages=[{"role": "user", "content": "x"}],
        client=_SlowWarmingClient(delay_s=2.0),
    )
    chat._cache_warmer.start()

    g = Grid(20, 110)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    footer = plain.split("\n")[-1]
    assert "warming" in footer
    chat._cache_warmer.close()


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
