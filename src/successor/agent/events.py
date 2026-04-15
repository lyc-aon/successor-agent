"""Events emitted by the query loop.

The loop is a state machine driven by `tick()`; each tick may emit
zero or more `ChatEvent`s via the on_event callback. Events are the
contract between the loop and its consumer (the chat UI, tests, the
burn driver).

Events are deliberately small frozen dataclasses (not enums or tuples)
so the consumer can pattern-match by type with isinstance() and the
type checker can prove exhaustiveness.

This mirrors free-code's `yield`-based message stream but adapted to
the sync frame-driven model: instead of yielding through an async
generator, the loop pushes events into a callback. Same information
flow, different plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..bash.cards import ToolCard
from .log import BoundaryMarker


# ─── Streaming events (one per tick during model generation) ───


@dataclass(frozen=True, slots=True)
class StreamStarted:
    """Model started producing tokens. The reasoning phase begins."""
    pass


@dataclass(frozen=True, slots=True)
class ReasoningChars:
    """The model is still in the reasoning phase. delta is char count."""
    delta: int


@dataclass(frozen=True, slots=True)
class ContentChunk:
    """A user-visible content delta from the model. text is the delta."""
    text: str


@dataclass(frozen=True, slots=True)
class StreamCommitted:
    """The model finished. full_text is the committed assistant message."""
    full_text: str
    bash_blocks_detected: int = 0
    usage: dict | None = None


@dataclass(frozen=True, slots=True)
class StreamFailed:
    """Stream errored out (network, server, prompt-too-long, ...)."""
    message: str
    is_prompt_too_long: bool = False


# ─── Tool execution events ───


@dataclass(frozen=True, slots=True)
class BashBlockDetected:
    """A fenced ```bash``` block completed during streaming.

    Carries the raw command extracted from the block. The loop will
    queue this for execution after StreamCommitted lands.
    """
    command: str
    block_index: int  # 0-based index within the assistant message


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """Loop is about to execute a parsed bash command."""
    command: str
    risk: str  # "safe" | "mutating" | "dangerous"


@dataclass(frozen=True, slots=True)
class ToolCompleted:
    """A bash command finished. card has output, exit_code, duration."""
    card: ToolCard


@dataclass(frozen=True, slots=True)
class ToolRefused:
    """A dangerous bash command was refused before execution."""
    card: ToolCard
    reason: str


# ─── Compaction events ───


@dataclass(frozen=True, slots=True)
class CompactionStarted:
    """Autocompact is about to fire. Lets the UI animate the fold."""
    pre_compact_tokens: int
    rounds_to_summarize: int
    reason: Literal["auto", "manual", "reactive"]


@dataclass(frozen=True, slots=True)
class Compacted:
    """A compaction event finished successfully.

    The renderer should:
      1. Animate the old rounds folding into the boundary line
      2. Render the boundary line as a permanent visual divider
      3. Show a toast: "compacted N tokens → M tokens"
    """
    boundary: BoundaryMarker


@dataclass(frozen=True, slots=True)
class CompactionFailed:
    """Compaction errored. Carries the reason and whether the loop
    should retry (False if circuit breaker tripped)."""
    reason: str
    will_retry: bool


# ─── Lifecycle events ───


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """A new query loop iteration is starting. turn_count is 1-based."""
    turn_count: int


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """The loop reached an idle state — no tools to execute, no more
    work to do for this user message."""
    turn_count: int
    final_token_count: int


@dataclass(frozen=True, slots=True)
class BlockingLimitReached:
    """The loop refused to call the API because the context is over
    the blocking threshold even after compaction."""
    token_count: int
    window_size: int


@dataclass(frozen=True, slots=True)
class MaxTurnsReached:
    """The loop hit max_turns without reaching idle. Safety bail-out."""
    turn_count: int


@dataclass(frozen=True, slots=True)
class LoopErrored:
    """A non-recoverable error in the loop. The loop transitions to IDLE."""
    message: str


@dataclass(frozen=True, slots=True)
class TransientRetry:
    """A transient error occurred before any content was delivered.

    The loop is backing off and retrying the stream. The UI should
    suppress the error display and optionally show a retry indicator.
    """
    attempt: int
    max_attempts: int
    delay_s: float
    reason: str


# ─── Type alias for the consumer's callback signature ───


ChatEvent = (
    StreamStarted | ReasoningChars | ContentChunk | StreamCommitted | StreamFailed
    | BashBlockDetected | ToolStarted | ToolCompleted | ToolRefused
    | CompactionStarted | Compacted | CompactionFailed
    | TransientRetry
    | TurnStarted | TurnCompleted
    | BlockingLimitReached | MaxTurnsReached | LoopErrored
)
