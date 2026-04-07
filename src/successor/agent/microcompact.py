"""Microcompact — cheap, stateless, time-based clearing of stale tool results.

The premise: tool results are usually relevant only for the round
they were produced in. After several more rounds (or after a long
idle period), the model has likely already digested the result and
the raw content is dead weight in the context window.

Free-code's microcompact has two paths:
  1. Time-based: clear tool results when the cache has expired (idle
     > 60 min — assumes Anthropic's prompt cache TTL).
  2. cache_edits: hot-edit the cached prefix to drop tool results
     without reprocessing. Anthropic-API-only.

We implement only the time-based path because:
  - llama.cpp's KV cache is local and free; there's no remote cache
    to keep alive.
  - The "kept count" is the cleaner trigger anyway: clear tool
    results past the N most recent, regardless of wall clock.

The function is PURE: takes a MessageLog, returns a NEW MessageLog
with old tool results' output replaced by a placeholder. No I/O,
no global state, no side effects. Tested standalone.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Iterable

from .log import ApiRound, LogMessage, MessageLog


# ─── Constants ───

# Default kept count — how many tool results to preserve verbatim.
# Anything older becomes a placeholder. Numbers tuned for Successor:
# 8 keeps recent context for the model to reference, beyond that the
# model has usually moved on.
DEFAULT_KEEP_TOOL_RESULTS: int = 8

# Default idle threshold — how long the most recent message must be
# old before time-based clearing fires.
DEFAULT_IDLE_THRESHOLD_S: float = 60.0 * 60.0  # 60 minutes

# Placeholder content used to replace cleared tool output. Stays
# short (a few tokens) so the cleared message doesn't waste budget.
CLEARED_PLACEHOLDER: str = "[tool result cleared during compaction]"


# ─── Public entry point ───


def microcompact(
    log: MessageLog,
    *,
    keep_recent: int = DEFAULT_KEEP_TOOL_RESULTS,
    idle_threshold_s: float | None = None,
    now: float | None = None,
) -> tuple[MessageLog, int]:
    """Clear stale tool results from the log.

    Two trigger conditions (both checked):
      1. The number of tool result messages exceeds keep_recent →
         clear all but the most recent keep_recent.
      2. The most recent message is older than idle_threshold_s →
         clear ALL tool results (the user has clearly walked away).

    Returns (new_log, n_cleared). Pure function — the input log is
    unchanged. n_cleared is 0 if nothing needed clearing, useful for
    diagnostics ("microcompact ran but did nothing").

    Implementation note: we deep-copy the rounds list and rebuild
    affected messages via LogMessage.replace_output(). The
    AttachmentRegistry is shared by reference because attachments
    aren't being changed by microcompact (only their content is
    being cleared).
    """
    if log.is_empty():
        return (log, 0)

    now = now if now is not None else time.monotonic()

    # ─── Decide which tool results to clear ───
    tool_msg_indices = list(_iter_tool_message_positions(log))
    if not tool_msg_indices:
        return (log, 0)

    # Trigger 1: count-based — clear oldest beyond keep_recent
    to_clear: set[tuple[int, int]] = set()
    if len(tool_msg_indices) > keep_recent:
        to_clear.update(tool_msg_indices[: -keep_recent])

    # Trigger 2: time-based — if user has been idle, clear EVERYTHING
    if idle_threshold_s is not None:
        latest_at = _latest_message_time(log)
        if latest_at > 0 and (now - latest_at) >= idle_threshold_s:
            to_clear.update(tool_msg_indices)

    if not to_clear:
        return (log, 0)

    # ─── Build the new log ───
    new_rounds: list[ApiRound] = []
    for ri, round in enumerate(log.rounds):
        new_round = ApiRound(
            started_at=round.started_at,
            token_estimate=0,  # invalidated; loop refreshes after
        )
        for mi, msg in enumerate(round.messages):
            if (ri, mi) in to_clear:
                # Already-cleared messages (placeholder content) stay as-is
                if msg.tool_card and msg.tool_card.output == CLEARED_PLACEHOLDER:
                    new_round.append(msg)
                else:
                    new_round.append(msg.replace_output(CLEARED_PLACEHOLDER))
            else:
                new_round.append(msg)
        new_rounds.append(new_round)

    # Number cleared = the number of (ri, mi) pairs we actually mutated
    # this call, not pre-existing placeholders.
    n_cleared_now = sum(
        1 for (ri, mi) in to_clear
        if log.rounds[ri].messages[mi].tool_card is not None
        and log.rounds[ri].messages[mi].tool_card.output != CLEARED_PLACEHOLDER
    )

    new_log = MessageLog(
        rounds=new_rounds,
        attachments=log.attachments,
        system_prompt=log.system_prompt,
    )
    return (new_log, n_cleared_now)


# ─── Helpers ───


def _iter_tool_message_positions(log: MessageLog) -> Iterable[tuple[int, int]]:
    """Yield (round_index, msg_index) for every message that has a
    tool_card attached. Order is chronological (oldest first)."""
    for ri, round in enumerate(log.rounds):
        for mi, msg in enumerate(round.messages):
            if msg.tool_card is not None:
                yield (ri, mi)


def _latest_message_time(log: MessageLog) -> float:
    """The most recent message's `created_at`. Returns 0 if log is empty."""
    latest = 0.0
    for r in log.rounds:
        for m in r.messages:
            if m.created_at > latest:
                latest = m.created_at
    return latest
