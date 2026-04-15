"""QueryLoop — the agent loop, as a tick-driven state machine.

The chat is a 30 FPS frame-driven App, NOT an async runtime, so the
loop is built as a state machine that advances on `tick()` calls
rather than as an async generator. Same flow as free-code's
`queryLoop` but pull-based instead of push-based.

State diagram:

    IDLE ──user_submit──▶ COMPACTING ──no_compact──▶ STREAMING
                              │                          │
                              │                       (model streams)
                              │                          │
                              └──compact_done──┐    StreamCommitted
                                               │          │
                                       ┌───────┘          │
                                       ▼                  ▼
                                   STREAMING       EXECUTING_TOOLS
                                                   ↓ (run all bash blocks)
                                                   │
                              ┌────────────────────┘
                              │
                              ▼
                          turn_count++
                              │
                       continue if more bash
                       expected, else IDLE

Each transition emits one or more ChatEvents via the on_event
callback. The chat consumes those events to update its UI (commit
messages, render tool cards, animate compaction folds, etc.).

The loop owns:
  - the MessageLog (the conversation state)
  - the BudgetTracker (token budget + circuit breaker + recompact chain)
  - the TokenCounter (for token estimates)
  - the current ChatStream (None when idle)
  - the bash detector (None when idle)
  - the pending bash blocks queue (filled by the detector, drained
    by the executor)
  - the LoopState enum (current phase)

The loop does NOT own:
  - rendering (the chat does)
  - input parsing (the chat does)
  - the LlamaCppClient (passed in)

Design choice: tool execution is SYNCHRONOUS in v0. dispatch_bash()
is a blocking subprocess.run() call, so we just call it inline
during the EXECUTING_TOOLS state. Concurrent execution comes later
when we have an asyncio event loop or a thread pool. For now,
single-tool-at-a-time keeps the state machine simple and correct.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..bash import (
    DangerousCommandRefused,
    dispatch_bash,
    preview_bash,
)
from ..providers.llama import (
    ChatStream,
    ContentChunk,
    LlamaCppClient,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)
from .bash_stream import BashStreamDetector
from .budget import BudgetTracker
from .compact import (
    CompactionError,
    DEFAULT_KEEP_RECENT_ROUNDS,
    can_compact_log,
    compact,
    normalized_keep_recent_rounds,
)
from .events import (
    BashBlockDetected,
    BlockingLimitReached,
    Compacted,
    CompactionFailed,
    CompactionStarted,
    ContentChunk as ContentChunkEv,
    LoopErrored,
    MaxTurnsReached,
    ReasoningChars,
    StreamCommitted,
    StreamFailed,
    StreamStarted as StreamStartedEv,
    ToolCompleted,
    ToolRefused,
    ToolStarted,
    TransientRetry,
    TurnCompleted,
    TurnStarted,
)
from .log import LogMessage, MessageLog
from .microcompact import microcompact
from .tokens import TokenCounter


# ─── Constants ───

DEFAULT_MAX_TURNS: int = 50

# Whether tool execution starts AS bash blocks are detected, or only
# after the stream commits. v0 = after-commit only; in-flight tool
# dispatch comes later when we wire async/threading.
EXECUTE_TOOLS_DURING_STREAM: bool = False

# Pre-stream retry: max attempts and backoff base.
MAX_TRANSIENT_RETRIES: int = 3
TRANSIENT_BACKOFF_BASE_S: float = 1.0


def is_transient_stream_error(msg: str) -> bool:
    """Classify a StreamError message as transient (retry-able) or fatal.

    Only errors that occur before any content has been delivered to the
    user are safe to retry — once content streams, retrying would produce
    a different response the user already saw part of.  This classifier
    is used *after* confirming zero content was delivered.
    """
    lower = msg.lower()
    if "connection refused" in lower or "errno 111" in lower:
        return True
    if "name or service not known" in lower or "nodename nor servname" in lower:
        return True
    if "temporary failure in name resolution" in lower:
        return True
    if "network is unreachable" in lower:
        return True
    if "timed out" in lower or "timeout" in lower:
        return True
    if "http 429" in lower or "too many requests" in lower:
        return True
    if "http 503" in lower or "service unavailable" in lower:
        return True
    if "connection reset" in lower or "broken pipe" in lower:
        return True
    return False


# ─── Loop state ───


class LoopPhase(Enum):
    """Discrete phases the loop can be in.

    The chat advances the loop by calling `tick()`, which inspects
    the current phase and transitions to the next.
    """
    IDLE = "idle"
    COMPACTING = "compacting"
    STREAMING = "streaming"
    EXECUTING_TOOLS = "executing_tools"
    DONE = "done"  # final terminal state — set after Done/Error events


# ─── The loop ───


@dataclass
class QueryLoop:
    """The agent loop, owned by the chat (or by tests / burn driver).

    Construction:
        loop = QueryLoop(
            log=existing_message_log,
            client=llama_cpp_client,
            counter=TokenCounter(endpoint=client),
            budget=BudgetTracker(budget=ContextBudget(window=50_000)),
            on_event=chat._handle_loop_event,
            max_turns=50,
        )

    Driving:
        # When the user submits a message:
        loop.start(user_text)

        # On each frame (or each tick from a test):
        loop.tick()

        # Check whether the loop is finished:
        if loop.phase in (LoopPhase.IDLE, LoopPhase.DONE):
            ...

    Events flow OUT through `on_event(ev)`. The loop never reads
    from the chat — communication is one-way pulled by the consumer.
    """

    log: MessageLog
    client: LlamaCppClient
    counter: TokenCounter
    budget: BudgetTracker = field(default_factory=BudgetTracker)
    on_event: Callable[[object], None] = lambda ev: None
    max_turns: int = DEFAULT_MAX_TURNS

    # ─── Mutable runtime state ───
    phase: LoopPhase = LoopPhase.IDLE
    turn_count: int = 0
    _stream: ChatStream | None = None
    _bash_detector: BashStreamDetector | None = None
    _pending_bash: list[str] = field(default_factory=list)
    _stream_content_buf: list[str] = field(default_factory=list)
    _stream_reasoning_chars: int = 0
    _last_error: str = ""
    _transient_retry_count: int = 0

    # Test/diagnostic introspection
    _started_at: float = 0.0
    _last_event_at: float = 0.0

    # ─── Public driving API ───

    def start(self, user_text: str, *, now: float | None = None) -> None:
        """Begin a new turn with a user message.

        The loop:
          1. Appends the user message to the log
          2. Refreshes token estimates
          3. Transitions to COMPACTING (which decides whether to
             actually compact based on the budget)

        It is an error to call start() while the loop is not IDLE.
        """
        if self.phase not in (LoopPhase.IDLE, LoopPhase.DONE):
            raise RuntimeError(
                f"cannot start a new turn while loop is in {self.phase.value}"
            )
        now_t = now if now is not None else time.monotonic()
        self._started_at = now_t

        self.log.begin_round(started_at=now_t)
        self.log.append_to_current_round(LogMessage(
            role="user", content=user_text, created_at=now_t,
        ))
        self.counter.refresh_round_estimates(self.log)

        self.turn_count += 1
        self._transient_retry_count = 0
        self._emit(TurnStarted(turn_count=self.turn_count))

        self.phase = LoopPhase.COMPACTING

    def tick(self) -> None:
        """Advance the loop one step. Called from the chat's on_tick.

        Each tick may transition the loop's phase 0 or more times and
        emit 0 or more events. Tick is non-blocking for streaming
        (consumes pending events from the ChatStream queue) and
        blocking for compaction + tool dispatch (those are HTTP/
        subprocess calls that return quickly enough).

        Idempotent in IDLE/DONE — safe to tick from a frame loop
        even when the loop has nothing to do.
        """
        if self.phase == LoopPhase.IDLE or self.phase == LoopPhase.DONE:
            return

        if self.phase == LoopPhase.COMPACTING:
            self._tick_compacting()
            return

        if self.phase == LoopPhase.STREAMING:
            self._tick_streaming()
            return

        if self.phase == LoopPhase.EXECUTING_TOOLS:
            self._tick_executing_tools()
            return

    def cancel(self) -> None:
        """Cancel any in-flight stream and return to IDLE.

        Used by the chat when the user presses Ctrl+G mid-stream.
        Does NOT roll back the user message — that stays in the log.
        """
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self._bash_detector = None
        self._pending_bash.clear()
        self._stream_content_buf.clear()
        self._stream_reasoning_chars = 0
        self.phase = LoopPhase.IDLE

    # ─── Per-phase tick handlers ───

    def _tick_compacting(self) -> None:
        """COMPACTING: run microcompact (always) + maybe autocompact,
        then transition to STREAMING (or BlockingLimitReached if still
        over the limit after compaction).
        """
        # 1. Microcompact (cheap, always)
        new_log, n_cleared = microcompact(self.log)
        if n_cleared > 0:
            self.log = new_log

        # 2. Refresh token estimates
        self.counter.refresh_round_estimates(self.log)
        used = self.counter.count_log(self.log)
        self.budget.observe(used)

        # 3. Should we autocompact?
        decision, reason = self.budget.should_attempt_compaction(used, self.turn_count)
        if decision and can_compact_log(self.log):
            keep_recent = normalized_keep_recent_rounds(
                self.log.round_count,
                keep_recent_rounds=DEFAULT_KEEP_RECENT_ROUNDS,
            )
            self._emit(CompactionStarted(
                pre_compact_tokens=used,
                rounds_to_summarize=self.log.round_count - keep_recent,
                reason="auto",
            ))
            try:
                new_log, boundary = compact(
                    self.log, self.client,
                    counter=self.counter,
                    reason="auto",
                )
                self.log = new_log
                self.budget.note_compaction_success(self.turn_count)
                self._emit(Compacted(boundary=boundary))
                # Refresh after compaction
                self.counter.refresh_round_estimates(self.log)
                used = self.counter.count_log(self.log)
                self.budget.observe(used)
            except (CompactionError, ValueError) as exc:
                self.budget.note_compaction_failure(self.turn_count)
                self._emit(CompactionFailed(
                    reason=str(exc),
                    will_retry=not self.budget.circuit_breaker.tripped,
                ))
                # Continue anyway — the API call may still succeed if
                # we're not over the blocking limit yet

        # 4. Blocking limit preempt
        if self.budget.budget.over_blocking_limit(used):
            self._emit(BlockingLimitReached(
                token_count=used,
                window_size=self.budget.budget.window,
            ))
            self.phase = LoopPhase.DONE
            return

        # 5. Begin streaming
        self._begin_stream()

    def _begin_stream(self) -> None:
        """Open a chat stream and transition to STREAMING."""
        try:
            self._stream = self.client.stream_chat(self.log.api_messages())
        except Exception as exc:
            self._emit(StreamFailed(message=str(exc)))
            self._emit(LoopErrored(message=str(exc)))
            self.phase = LoopPhase.DONE
            return

        self._bash_detector = BashStreamDetector()
        self._pending_bash = []
        self._stream_content_buf = []
        self._stream_reasoning_chars = 0
        self.phase = LoopPhase.STREAMING
        self._emit(StreamStartedEv())

    def _tick_streaming(self) -> None:
        """STREAMING: drain pending stream events, feed bash detector,
        watch for completion."""
        if self._stream is None:
            self.phase = LoopPhase.IDLE
            return

        events = self._stream.drain()
        for ev in events:
            if isinstance(ev, StreamStarted):
                pass  # already emitted our own
            elif isinstance(ev, ReasoningChunk):
                self._stream_reasoning_chars += len(ev.text)
                self._emit(ReasoningChars(delta=len(ev.text)))
            elif isinstance(ev, ContentChunk):
                self._stream_content_buf.append(ev.text)
                self._emit(ContentChunkEv(text=ev.text))
                # Feed the bash detector
                if self._bash_detector is not None:
                    new_blocks = self._bash_detector.feed(ev.text)
                    for blk in new_blocks:
                        self._pending_bash.append(blk)
                        self._emit(BashBlockDetected(
                            command=blk,
                            block_index=len(self._pending_bash) - 1,
                        ))
            elif isinstance(ev, StreamEnded):
                # Flush any final fence the detector was holding
                if self._bash_detector is not None:
                    final_blocks = self._bash_detector.flush()
                    for blk in final_blocks:
                        self._pending_bash.append(blk)
                        self._emit(BashBlockDetected(
                            command=blk,
                            block_index=len(self._pending_bash) - 1,
                        ))
                # Commit the assistant message to the log
                full = "".join(self._stream_content_buf)
                self.log.append_to_current_round(LogMessage(
                    role="assistant", content=full, created_at=time.monotonic(),
                ))
                self.counter.refresh_round_estimates(self.log)
                self._emit(StreamCommitted(
                    full_text=full,
                    bash_blocks_detected=len(self._pending_bash),
                    usage=ev.usage,
                ))
                self._stream = None
                # Transition based on whether we have tools to execute
                if self._pending_bash:
                    self.phase = LoopPhase.EXECUTING_TOOLS
                else:
                    self._end_turn()
                return
            elif isinstance(ev, StreamError):
                msg = ev.message or ""
                is_ptl = "prompt is too long" in msg.lower() or "context window" in msg.lower()
                self._emit(StreamFailed(message=msg, is_prompt_too_long=is_ptl))
                self._stream = None
                if is_ptl:
                    # Reactive compact: try to recover by compacting NOW
                    self._reactive_compact_recovery(msg)
                    return

                # Pre-stream transient retry: if no content has been
                # delivered to the user yet, the error happened before
                # any visible output.  Safe to back off and retry — the
                # user never saw a partial response.
                no_content = not self._stream_content_buf
                if (
                    no_content
                    and is_transient_stream_error(msg)
                    and self._transient_retry_count < MAX_TRANSIENT_RETRIES
                ):
                    self._transient_retry_count += 1
                    delay = TRANSIENT_BACKOFF_BASE_S * (
                        2 ** (self._transient_retry_count - 1)
                    )
                    self._emit(TransientRetry(
                        attempt=self._transient_retry_count,
                        max_attempts=MAX_TRANSIENT_RETRIES,
                        delay_s=delay,
                        reason=msg,
                    ))
                    time.sleep(delay)
                    self._begin_stream()
                    return

                self._emit(LoopErrored(message=msg))
                self.phase = LoopPhase.DONE
                return

    def _reactive_compact_recovery(self, original_error: str) -> None:
        """Reactive compact path: the API said prompt-too-long even
        though our budget tracker said we were under threshold. Force
        a compaction and retry the stream.

        Mirrors free-code's reactive compact at query.ts:1119-1165.
        """
        if self.budget.circuit_breaker.tripped:
            self._emit(LoopErrored(message=f"reactive compact unavailable (circuit tripped): {original_error}"))
            self.phase = LoopPhase.DONE
            return
        if not can_compact_log(self.log):
            self._emit(LoopErrored(
                message=(
                    "reactive compact unavailable "
                    "(need at least one older round to summarize and one recent round to keep): "
                    f"{original_error}"
                )
            ))
            self.phase = LoopPhase.DONE
            return

        used_before = self.counter.count_log(self.log)
        keep_recent = normalized_keep_recent_rounds(
            self.log.round_count,
            keep_recent_rounds=DEFAULT_KEEP_RECENT_ROUNDS,
        )
        self._emit(CompactionStarted(
            pre_compact_tokens=used_before,
            rounds_to_summarize=self.log.round_count - keep_recent,
            reason="reactive",
        ))
        try:
            new_log, boundary = compact(
                self.log, self.client,
                counter=self.counter,
                reason="reactive",
            )
            self.log = new_log
            self.budget.note_compaction_success(self.turn_count)
            self._emit(Compacted(boundary=boundary))
            self.counter.refresh_round_estimates(self.log)
            self.budget.observe(self.counter.count_log(self.log))
            # Retry the stream
            self._begin_stream()
        except (CompactionError, ValueError) as exc:
            self.budget.note_compaction_failure(self.turn_count)
            self._emit(CompactionFailed(reason=str(exc), will_retry=False))
            self._emit(LoopErrored(message=f"reactive compact failed: {exc}"))
            self.phase = LoopPhase.DONE

    def _tick_executing_tools(self) -> None:
        """EXECUTING_TOOLS: drain the pending bash queue serially.

        v0 is synchronous: dispatch each bash command in order, append
        the result to the log, emit ToolCompleted. When the queue is
        empty, decide whether to continue (model is expected to react
        to the results) or end the turn.
        """
        # Pull one command per tick so we yield control to the chat's
        # frame loop. The chat can paint progress between commands.
        if not self._pending_bash:
            # All commands executed — feed results back to model on
            # next iteration. The standard pattern is "increment turn,
            # loop continues to compaction → streaming". For v0 we
            # END the turn after a single tool batch — multi-turn
            # tool conversations come when the agent loop is fully wired.
            self._end_turn()
            return

        cmd = self._pending_bash.pop(0)
        # Preview to get risk class
        try:
            preview = preview_bash(cmd)
        except Exception:
            preview = None

        risk_label = preview.risk if preview is not None else "safe"
        self._emit(ToolStarted(command=cmd, risk=risk_label))

        try:
            card = dispatch_bash(cmd)
        except DangerousCommandRefused as exc:
            self._emit(ToolRefused(card=exc.card, reason=exc.reason))
            # Append the refused card to the log so the model knows
            self.log.append_to_current_round(LogMessage(
                role="tool", content="", tool_card=exc.card,
                created_at=time.monotonic(),
            ))
            return
        except Exception as exc:
            self._emit(LoopErrored(message=f"tool dispatch failed: {exc}"))
            self.phase = LoopPhase.DONE
            return

        self._emit(ToolCompleted(card=card))
        self.log.append_to_current_round(LogMessage(
            role="tool", content="", tool_card=card,
            created_at=time.monotonic(),
        ))
        self.counter.refresh_round_estimates(self.log)

    def _end_turn(self) -> None:
        """Wrap up the current turn — emit TurnCompleted, return to IDLE."""
        used = self.counter.count_log(self.log)
        self._emit(TurnCompleted(
            turn_count=self.turn_count,
            final_token_count=used,
        ))
        if self.turn_count >= self.max_turns:
            self._emit(MaxTurnsReached(turn_count=self.turn_count))
            self.phase = LoopPhase.DONE
            return
        self.phase = LoopPhase.IDLE

    # ─── Event emission ───

    def _emit(self, event: object) -> None:
        self._last_event_at = time.monotonic()
        try:
            self.on_event(event)
        except Exception:
            # The chat's event handler should NEVER raise. If it does,
            # we swallow rather than corrupt loop state.
            pass

    # ─── Diagnostic helpers ───

    def stats(self) -> dict:
        """Return a snapshot of internal state for diagnostics + tests."""
        return {
            "phase": self.phase.value,
            "turn_count": self.turn_count,
            "rounds": self.log.round_count,
            "messages": self.log.total_messages(),
            "tokens": self.counter.count_log(self.log),
            "fill_pct": self.budget.budget.fill_pct(self.budget.last_observed_tokens),
            "compactions_total": self.budget.compactions_total,
            "compactions_failed": self.budget.compactions_failed,
            "circuit_tripped": self.budget.circuit_breaker.tripped,
            "pending_bash": len(self._pending_bash),
            "last_error": self._last_error,
        }
