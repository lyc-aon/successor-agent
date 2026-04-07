"""Agent loop + compaction subsystem.

Public surface (incremental — not all built yet):
    log.py            data shapes (LogMessage, ApiRound, MessageLog, BoundaryMarker)
    events.py         ChatEvent ADT yielded by the loop
    tokens.py         TokenCounter — /tokenize endpoint + char heuristic
    budget.py         ContextBudget + CircuitBreaker + RecompactChain
    microcompact.py   time-based stale tool result clearing
    compact.py        autocompact via llama.cpp summarization + PTL retry
    bash_stream.py    fenced bash block detector for streaming content
    loop.py           QueryLoop state machine driven by tick()
"""

from __future__ import annotations

from .bash_stream import BashStreamDetector
from .budget import (
    BudgetTracker,
    CircuitBreaker,
    ContextBudget,
    RecompactChain,
    ThresholdState,
)
from .compact import (
    CompactionError,
    CompactionClient,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_SUMMARY_INSTRUCTIONS,
    DEFAULT_SUMMARY_MAX_TOKENS,
    MIN_ROUNDS_TO_COMPACT,
    PromptTooLongError,
    compact,
)
from .events import (
    BashBlockDetected,
    BlockingLimitReached,
    ChatEvent,
    Compacted,
    CompactionFailed,
    CompactionStarted,
    ContentChunk,
    LoopErrored,
    MaxTurnsReached,
    ReasoningChars,
    StreamCommitted,
    StreamFailed,
    StreamStarted,
    ToolCompleted,
    ToolRefused,
    ToolStarted,
    TurnCompleted,
    TurnStarted,
)
from .log import (
    ApiRound,
    AttachmentRegistry,
    BoundaryMarker,
    LogMessage,
    MessageLog,
    Role,
)
from .loop import (
    DEFAULT_MAX_TURNS,
    LoopPhase,
    QueryLoop,
)
from .microcompact import (
    CLEARED_PLACEHOLDER,
    DEFAULT_IDLE_THRESHOLD_S,
    DEFAULT_KEEP_TOOL_RESULTS,
    microcompact,
)
from .tokens import (
    DEFAULT_CACHE_SIZE,
    HEURISTIC_CHARS_PER_TOKEN,
    TokenCounter,
    TokenizerEndpoint,
)


__all__ = [
    # Log shapes
    "ApiRound",
    "AttachmentRegistry",
    "BoundaryMarker",
    "LogMessage",
    "MessageLog",
    "Role",
    # Events
    "BashBlockDetected",
    "BlockingLimitReached",
    "ChatEvent",
    "Compacted",
    "CompactionFailed",
    "CompactionStarted",
    "ContentChunk",
    "LoopErrored",
    "MaxTurnsReached",
    "ReasoningChars",
    "StreamCommitted",
    "StreamFailed",
    "StreamStarted",
    "ToolCompleted",
    "ToolRefused",
    "ToolStarted",
    "TurnCompleted",
    "TurnStarted",
    # Tokens / budget / breaker
    "BudgetTracker",
    "CircuitBreaker",
    "ContextBudget",
    "DEFAULT_CACHE_SIZE",
    "HEURISTIC_CHARS_PER_TOKEN",
    "RecompactChain",
    "ThresholdState",
    "TokenCounter",
    "TokenizerEndpoint",
    # Compaction
    "CLEARED_PLACEHOLDER",
    "CompactionClient",
    "CompactionError",
    "DEFAULT_IDLE_THRESHOLD_S",
    "DEFAULT_KEEP_RECENT_ROUNDS",
    "DEFAULT_KEEP_TOOL_RESULTS",
    "DEFAULT_SUMMARY_INSTRUCTIONS",
    "DEFAULT_SUMMARY_MAX_TOKENS",
    "MIN_ROUNDS_TO_COMPACT",
    "PromptTooLongError",
    "compact",
    "microcompact",
    # Bash stream
    "BashStreamDetector",
    # Loop
    "DEFAULT_MAX_TURNS",
    "LoopPhase",
    "QueryLoop",
]
