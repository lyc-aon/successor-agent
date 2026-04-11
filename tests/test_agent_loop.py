"""Tests for agent/loop.py — QueryLoop state machine.

Uses the same mock client / mock stream pattern as test_agent_compact.
The loop is driven by tick() so tests can step through phase
transitions deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field


from successor.agent.budget import BudgetTracker, ContextBudget
from successor.agent.events import (
    BashBlockDetected,
    BlockingLimitReached,
    Compacted,
    CompactionStarted,
    ContentChunk as ContentChunkEv,
    LoopErrored,
    MaxTurnsReached,
    StreamCommitted,
    StreamFailed,
    StreamStarted as StreamStartedEv,
    ToolCompleted,
    ToolRefused,
    ToolStarted,
    TurnCompleted,
    TurnStarted,
)
from successor.agent.log import LogMessage, MessageLog
from successor.agent.loop import LoopPhase, QueryLoop
from successor.agent.tokens import TokenCounter
from successor.providers.llama import (
    ContentChunk,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)


# ─── Mock stream + client (same shape as test_agent_compact) ───


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


@dataclass
class _MockClient:
    streams: list[_MockStream] = field(default_factory=list)
    call_count: int = 0
    last_messages: list = field(default_factory=list)
    base_url: str = "http://mock"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None):
        self.call_count += 1
        self.last_messages = list(messages)
        idx = min(self.call_count - 1, len(self.streams) - 1)
        return self.streams[idx]


def _stream_text(text: str, reasoning: str = "") -> _MockStream:
    """Build a stream that emits reasoning + content + StreamEnded."""
    events = [StreamStarted()]
    if reasoning:
        events.append(ReasoningChunk(text=reasoning))
    events.append(ContentChunk(text=text))
    events.append(StreamEnded(finish_reason="stop", usage=None, timings=None))
    return _MockStream(events=events)


def _stream_with_bash(prefix_text: str, bash_command: str, suffix_text: str = "") -> _MockStream:
    """Stream a model response that contains a bash block."""
    full_text = f"{prefix_text}\n```bash\n{bash_command}\n```\n{suffix_text}"
    return _MockStream(events=[
        StreamStarted(),
        ContentChunk(text=full_text),
        StreamEnded(finish_reason="stop", usage=None, timings=None),
    ])


def _stream_with_error(msg: str) -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        StreamError(message=msg),
    ])


def _stream_with_ptl_then_summary(summary: str = "summary") -> tuple[_MockStream, _MockStream]:
    return (
        _MockStream(events=[
            StreamStarted(),
            StreamError(message="prompt is too long"),
        ]),
        _MockStream(events=[
            StreamStarted(),
            ContentChunk(text=summary),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]),
    )


# ─── Helpers ───


def _drive_to_idle(loop: QueryLoop, *, max_ticks: int = 1000) -> int:
    ticks = 0
    while loop.phase not in (LoopPhase.IDLE, LoopPhase.DONE):
        loop.tick()
        ticks += 1
        if ticks > max_ticks:
            raise RuntimeError(f"loop stuck in {loop.phase.value} after {ticks} ticks")
    return ticks


def _make_loop(
    streams: list[_MockStream] | None = None,
    *,
    window: int = 100_000,
    autocompact_buffer: int = 5_000,
    blocking_buffer: int = 1_000,
    max_turns: int = 5,
) -> tuple[QueryLoop, _MockClient, list]:
    client = _MockClient(streams=streams or [])
    counter = TokenCounter()  # heuristic only
    budget = BudgetTracker(budget=ContextBudget(
        window=window, warning_buffer=10_000,
        autocompact_buffer=autocompact_buffer,
        blocking_buffer=blocking_buffer,
    ))
    log = MessageLog(system_prompt="You are successor.")
    events: list = []
    loop = QueryLoop(
        log=log, client=client, counter=counter, budget=budget,
        on_event=events.append, max_turns=max_turns,
    )
    return loop, client, events


# ─── Initial state ───


def test_loop_starts_idle() -> None:
    loop, _, _ = _make_loop()
    assert loop.phase == LoopPhase.IDLE
    assert loop.turn_count == 0


def test_tick_in_idle_is_noop() -> None:
    loop, _, events = _make_loop()
    loop.tick()
    loop.tick()
    assert loop.phase == LoopPhase.IDLE
    assert events == []


def test_start_appends_user_message_and_increments_turn() -> None:
    loop, _, events = _make_loop(streams=[_stream_text("ok")])
    loop.start("hello")
    assert loop.turn_count == 1
    assert loop.log.total_messages() == 1
    assert isinstance(events[0], TurnStarted)


# ─── Happy path: simple Q&A ───


def test_simple_qa_runs_to_idle() -> None:
    loop, client, events = _make_loop(streams=[_stream_text("hi there")])
    loop.start("hello")
    ticks = _drive_to_idle(loop)
    assert ticks > 0
    assert loop.phase == LoopPhase.IDLE
    # API was called
    assert client.call_count == 1
    # Final log has user + assistant
    assert loop.log.total_messages() == 2
    assert any(isinstance(e, TurnStarted) for e in events)
    assert any(isinstance(e, StreamStartedEv) for e in events)
    assert any(isinstance(e, ContentChunkEv) for e in events)
    assert any(isinstance(e, StreamCommitted) for e in events)
    assert any(isinstance(e, TurnCompleted) for e in events)


def test_committed_message_lands_in_log() -> None:
    loop, _, _ = _make_loop(streams=[_stream_text("the answer")])
    loop.start("the question")
    _drive_to_idle(loop)
    msgs = list(loop.log.iter_messages())
    assert msgs[0].role == "user" and msgs[0].content == "the question"
    assert msgs[1].role == "assistant" and msgs[1].content == "the answer"


# ─── Bash block detection + tool execution ───


def test_loop_detects_bash_block_and_executes() -> None:
    loop, _, events = _make_loop(streams=[
        _stream_with_bash("Let me check.", "echo loop-test", "Done."),
    ])
    loop.start("show me echo")
    _drive_to_idle(loop)

    # Bash block was detected
    assert any(isinstance(e, BashBlockDetected) for e in events)
    # Tool started + completed
    assert any(isinstance(e, ToolStarted) for e in events)
    completed = [e for e in events if isinstance(e, ToolCompleted)]
    assert len(completed) == 1
    assert "loop-test" in completed[0].card.output
    # Tool message in the log
    tool_msgs = [m for m in loop.log.iter_messages() if m.tool_card]
    assert len(tool_msgs) == 1


def test_loop_dangerous_command_emits_refused() -> None:
    loop, _, events = _make_loop(streams=[
        _stream_with_bash("", "rm -rf /", ""),
    ])
    loop.start("nuke things")
    _drive_to_idle(loop)
    refused = [e for e in events if isinstance(e, ToolRefused)]
    assert len(refused) == 1
    assert refused[0].card.risk == "dangerous"


def test_loop_multiple_bash_blocks_execute_in_order() -> None:
    """A model response with two bash blocks → both executed in
    sequence, both ToolCompleted events emitted."""
    loop, _, events = _make_loop(streams=[
        _MockStream(events=[
            StreamStarted(),
            ContentChunk(text="```bash\necho first\n```\nthen\n```bash\necho second\n```"),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]),
    ])
    loop.start("two echos")
    _drive_to_idle(loop)
    completed = [e for e in events if isinstance(e, ToolCompleted)]
    assert len(completed) == 2
    assert "first" in completed[0].card.output
    assert "second" in completed[1].card.output


# ─── Error paths ───


def test_loop_stream_error_transitions_to_done() -> None:
    loop, _, events = _make_loop(streams=[_stream_with_error("network failure")])
    loop.start("hello")
    _drive_to_idle(loop)
    assert loop.phase == LoopPhase.DONE
    assert any(isinstance(e, StreamFailed) for e in events)
    assert any(isinstance(e, LoopErrored) for e in events)


def test_loop_ptl_error_triggers_reactive_compact() -> None:
    """A prompt-too-long stream error triggers reactive compaction
    if there are enough rounds. Mocked: PTL error → compact succeeds → retry."""
    ptl_stream, summary_stream = _stream_with_ptl_then_summary("compacted summary")
    final_stream = _stream_text("answer after compact")
    loop, client, events = _make_loop(streams=[
        ptl_stream,    # initial stream — PTL error
        summary_stream,  # compaction call
        final_stream,  # retry stream
    ])
    # Pre-load enough rounds so reactive compact has something to work with
    for i in range(10):
        loop.log.begin_round()
        loop.log.append_to_current_round(LogMessage(
            role="user", content=f"q{i}", created_at=float(i),
        ))
        loop.log.append_to_current_round(LogMessage(
            role="assistant", content=f"a{i}", created_at=float(i),
        ))
    loop.start("triggering ptl")
    _drive_to_idle(loop)
    assert any(isinstance(e, CompactionStarted) and e.reason == "reactive" for e in events)
    assert any(isinstance(e, Compacted) for e in events)


def test_loop_ptl_with_too_few_rounds_errors_out() -> None:
    """If PTL fires but there aren't enough rounds to compact, the
    loop must exit with LoopErrored — NOT infinite-loop."""
    loop, _, events = _make_loop(streams=[
        _stream_with_error("prompt is too long"),
    ])
    loop.start("hi")  # only 1 round, far below MIN_ROUNDS_TO_COMPACT
    _drive_to_idle(loop)
    assert loop.phase == LoopPhase.DONE
    assert any(isinstance(e, LoopErrored) for e in events)


# ─── Compaction integration ───


def test_loop_fires_autocompact_when_over_threshold() -> None:
    """When the budget says compact, the loop must run compaction
    and emit Compacted before streaming."""
    loop, client, events = _make_loop(
        streams=[
            _stream_text("compacted summary"),  # compaction call
            _stream_text("answer"),              # actual stream
        ],
        window=200,        # tiny window
        autocompact_buffer=20,
        blocking_buffer=5,
    )
    # Pre-load enough rounds + content to push over threshold
    for i in range(10):
        loop.log.begin_round()
        loop.log.append_to_current_round(LogMessage(
            role="user", content="x" * 200, created_at=float(i),
        ))
        loop.log.append_to_current_round(LogMessage(
            role="assistant", content="y" * 200, created_at=float(i),
        ))
    loop.start("trigger")
    _drive_to_idle(loop)
    assert any(isinstance(e, CompactionStarted) for e in events)
    assert any(isinstance(e, Compacted) for e in events)


def test_loop_blocking_limit_emits_event_and_stops() -> None:
    """If after compaction we're still over the blocking limit, the
    loop refuses to call the API and yields BlockingLimitReached."""
    # Hard to trigger naturally — directly load the log past the limit
    loop, client, events = _make_loop(
        streams=[],  # no streams needed; we never call the API
        window=100, autocompact_buffer=20, blocking_buffer=5,
    )
    # Start with a single huge round so the loop is far past the
    # blocking limit but still has too little structure to compact.
    loop.start("x" * 1000)
    _drive_to_idle(loop)
    assert any(isinstance(e, BlockingLimitReached) for e in events)
    assert loop.phase == LoopPhase.DONE


# ─── Cancel ───


def test_cancel_returns_to_idle() -> None:
    loop, _, _ = _make_loop(streams=[_stream_text("answer")])
    loop.start("hi")
    # Tick once to enter STREAMING
    loop.tick()
    loop.cancel()
    assert loop.phase == LoopPhase.IDLE


# ─── Max turns ───


def test_max_turns_stops_loop() -> None:
    """After hitting max_turns the loop emits MaxTurnsReached and DONE."""
    streams = [_stream_text(f"answer {i}") for i in range(5)]
    loop, _, events = _make_loop(streams=streams, max_turns=2)

    loop.start("first")
    _drive_to_idle(loop)
    assert loop.turn_count == 1
    assert loop.phase == LoopPhase.IDLE

    loop.start("second")
    _drive_to_idle(loop)
    assert loop.turn_count == 2
    # After turn 2, max_turns is reached → DONE
    assert loop.phase == LoopPhase.DONE
    assert any(isinstance(e, MaxTurnsReached) for e in events)


# ─── Stats / introspection ───


def test_stats_reflects_state() -> None:
    loop, _, _ = _make_loop(streams=[_stream_text("hi")])
    assert loop.stats()["phase"] == "idle"
    loop.start("hello")
    _drive_to_idle(loop)
    s = loop.stats()
    assert s["phase"] == "idle"
    assert s["turn_count"] == 1
    assert s["rounds"] == 1
    assert s["messages"] == 2
    assert s["tokens"] > 0
