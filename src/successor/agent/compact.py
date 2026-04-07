"""Full compaction — LLM-based summarization with PTL retry.

The expensive layer of the compaction pipeline. When the budget
tracker says we're past the autocompact threshold, this module:

  1. Sends the current rounds + a summarization prompt to llama.cpp
  2. Receives a textual summary of the older rounds
  3. Builds a new MessageLog with: BoundaryMarker + summary
     message + the most recent N rounds preserved verbatim
  4. Re-attaches the most-recent files via the AttachmentRegistry
  5. Returns the new log + the BoundaryMarker for the loop to yield

PTL recovery: if the API rejects the summarization request itself
because the prompt is too long, we drop the oldest 3 rounds and
retry — up to MAX_PTL_RETRIES times. This mirrors free-code's
truncateHeadForPTLRetry pattern but adapted to our round-based
truncation (which is cleaner because we never orphan tool calls).

The function is synchronous (blocking) because llama.cpp's HTTP
streaming is synchronous from our point of view — we open the
stream, drain it to completion, return. No asyncio. The chat's
frame-driven model handles the perceived concurrency by ticking
between compactions and other paint passes.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Protocol

from .log import (
    ApiRound,
    BoundaryMarker,
    LogMessage,
    MessageLog,
)
from .tokens import TokenCounter


# ─── Constants ───

# How many of the most recent rounds to preserve verbatim after
# compaction. The rest get summarized into one block.
DEFAULT_KEEP_RECENT_ROUNDS: int = 6

# How many rounds at minimum must exist BEFORE compaction is allowed
# to fire (no point compacting 2 rounds — there's nothing to compact).
MIN_ROUNDS_TO_COMPACT: int = 4

# Max PTL retry attempts before giving up
MAX_PTL_RETRIES: int = 3

# Per-PTL-retry: how many oldest rounds to drop
PTL_DROP_PER_RETRY: int = 3

# Max tokens the summary model is allowed to produce. Mirrors
# free-code's `summary_max_tokens = 20_000` but tuned for our
# typically-narrower contexts.
DEFAULT_SUMMARY_MAX_TOKENS: int = 16_000

# Temperature for summarization — lower than chat default because
# we want deterministic, factual summaries, not creative ones.
SUMMARY_TEMPERATURE: float = 0.2


# ─── Default summarization prompt ───
#
# Tuned for Qwen 3.5 distill. Stays explicit about what to preserve.
# Long enough to give the model clear marching orders, short enough
# to leave room for actual content.

DEFAULT_SUMMARY_INSTRUCTIONS = """\
You are summarizing a conversation between a user and an AI assistant
named successor. Your task is to produce a SINGLE SUMMARY that
captures everything important from the exchange so the assistant can
continue the conversation coherently after the original turns are
discarded.

Preserve in your summary:
  - Every concrete fact the user provided (names, paths, numbers,
    decisions, preferences, error messages, code snippets they pasted)
  - Every commitment the assistant made (TODOs, plans, next steps)
  - Every file path and directory that was mentioned or examined
  - Every command that was run and its key result
  - Any unresolved questions or open threads

Discard:
  - Greetings, acknowledgments, "let me think about this" filler
  - Step-by-step reasoning the assistant did out loud — keep only
    the conclusion, not the chain of thought
  - Output that was already digested into a follow-up answer

Format:
  - Plain prose paragraphs, no markdown headers, no bullet lists
  - First-person from the assistant's perspective ("I noted that...",
    "the user asked me to...")
  - Past tense
  - Maximum density: every sentence should add information

Begin your summary directly. Do not preface with "Here is the
summary" or any other meta-commentary.
"""


# ─── Exceptions ───


class CompactionError(Exception):
    """Compaction failed for a non-recoverable reason."""


class PromptTooLongError(CompactionError):
    """The summarization prompt itself was too long. The PTL retry
    loop catches this and truncates oldest rounds before retrying."""


# ─── Provider protocol ───


class CompactionClient(Protocol):
    """The minimum surface compact() needs from a chat client.

    Both LlamaCppClient and the test mock implement this. We don't
    depend on the concrete LlamaCppClient class so the tests can
    substitute a deterministic fake without monkey-patching.
    """
    def stream_chat(
        self,
        messages,  # iterable of dicts
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        extra: dict | None = None,
    ): ...  # returns a ChatStream-shaped thing


# ─── The summarization driver ───


def _drain_to_summary(stream) -> str:
    """Block until a stream completes, return the assembled content text.

    Mirrors what SuccessorChat does in _pump_stream but synchronous —
    we just want the final string. Stream-error → CompactionError.
    """
    from ..providers.llama import (
        ContentChunk,
        ReasoningChunk,
        StreamEnded,
        StreamError,
        StreamStarted,
    )
    chunks: list[str] = []
    deadline = time.monotonic() + 600.0  # 10 min — generous for big summaries
    while True:
        if time.monotonic() > deadline:
            stream.close()
            raise CompactionError("summarization stream timed out")
        events = stream.drain()
        for ev in events:
            if isinstance(ev, ContentChunk):
                chunks.append(ev.text)
            elif isinstance(ev, ReasoningChunk):
                pass  # discard reasoning — we only want the answer
            elif isinstance(ev, StreamEnded):
                full = "".join(chunks).strip()
                if not full:
                    raise CompactionError(
                        "summarization produced empty output "
                        "(model likely emitted only reasoning — "
                        "check max_tokens budget)"
                    )
                return full
            elif isinstance(ev, StreamError):
                msg = ev.message or ""
                if "prompt is too long" in msg.lower() or "context window" in msg.lower():
                    raise PromptTooLongError(msg)
                raise CompactionError(f"stream error: {msg}")
            elif isinstance(ev, StreamStarted):
                pass
        # No events drained this poll — sleep briefly to avoid spin
        time.sleep(0.05)


def _build_summary_prompt(
    log: MessageLog,
    *,
    rounds_to_summarize: list[ApiRound],
    instructions: str,
) -> list[dict[str, str]]:
    """Build the OpenAI-format messages list to send to the summarizer.

    The prompt is:
      [system] you are summarizing a conversation
      [user] here is the conversation: <serialized rounds>
             produce the summary now.

    We deliberately rebuild from scratch rather than reusing
    log.api_messages() so the system prompt is the SUMMARIZER prompt,
    not the chat's normal one.
    """
    # Serialize the rounds-to-summarize as plain text dialogue.
    # This is what the model summarizes.
    transcript_lines: list[str] = []
    for r in rounds_to_summarize:
        for m in r.messages:
            if m.is_boundary or m.is_summary:
                # Existing summary already in the log — include it as-is
                # so the new summary can build on it
                transcript_lines.append(f"[earlier summary] {m.content}")
                continue
            if m.tool_card is not None:
                card = m.tool_card
                transcript_lines.append(
                    f"[tool call: {card.verb}] $ {card.raw_command}"
                )
                if card.output:
                    out = card.output[:1000]
                    transcript_lines.append(f"[tool output] {out}")
                continue
            role_label = {
                "user": "user",
                "assistant": "assistant",
                "system": "system",
                "tool": "tool",
            }.get(m.role, m.role)
            transcript_lines.append(f"{role_label}: {m.content}")
    transcript = "\n\n".join(transcript_lines)

    return [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": (
                "Conversation to summarize:\n\n"
                "═══════════\n"
                f"{transcript}\n"
                "═══════════\n\n"
                "Produce the summary now. Begin directly — no preamble."
            ),
        },
    ]


# ─── Public entry point ───


def compact(
    log: MessageLog,
    client: CompactionClient,
    *,
    counter: TokenCounter,
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
    instructions: str = DEFAULT_SUMMARY_INSTRUCTIONS,
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    reason: str = "auto",
    now: float | None = None,
) -> tuple[MessageLog, BoundaryMarker]:
    """Compact a MessageLog by summarizing its older rounds.

    Returns (new_log, boundary). The new log has the structure:
      [boundary marker round]
      [summary message round]
      [recent rounds preserved verbatim, last keep_recent_rounds]
      [attachment hint round, if any files were tracked]

    Raises CompactionError on failure (empty summary, model error,
    PTL retries exhausted).

    Raises ValueError if there aren't enough rounds to compact.
    """
    now_t = now if now is not None else time.monotonic()

    # Refuse if there's nothing meaningful to compact
    if len(log.rounds) < MIN_ROUNDS_TO_COMPACT:
        raise ValueError(
            f"need at least {MIN_ROUNDS_TO_COMPACT} rounds to compact, "
            f"got {len(log.rounds)}"
        )

    # Pre-compact token count for the boundary marker
    pre_tokens = counter.count_log(log)

    # Split the log: rounds to summarize vs rounds to keep
    if keep_recent_rounds <= 0:
        keep_recent_rounds = 1
    if keep_recent_rounds >= len(log.rounds):
        # Nothing left to summarize — caller asked for too many recent
        keep_recent_rounds = max(1, len(log.rounds) // 2)

    rounds_to_summarize = log.rounds[: -keep_recent_rounds]
    rounds_to_keep = log.rounds[-keep_recent_rounds:]

    if not rounds_to_summarize:
        raise ValueError(
            f"after splitting, no rounds to summarize "
            f"(rounds={len(log.rounds)}, keep={keep_recent_rounds})"
        )

    # ─── Run the summarization with PTL retry ───
    summary_text = _summarize_with_ptl_retry(
        log=log,
        rounds_to_summarize=rounds_to_summarize,
        client=client,
        instructions=instructions,
        max_tokens=summary_max_tokens,
    )

    # ─── Build the new log ───
    new_log = MessageLog(
        rounds=[],  # we'll add the boundary + summary + kept rounds
        attachments=log.attachments,  # share by reference — not changed
        system_prompt=log.system_prompt,
    )

    # Boundary marker first (its own round)
    boundary = BoundaryMarker(
        happened_at=now_t,
        pre_compact_tokens=pre_tokens,
        post_compact_tokens=0,  # filled in below after recount
        rounds_summarized=len(rounds_to_summarize),
        summary_text=summary_text,
        reason=reason,
    )
    new_log.insert_boundary(boundary, summary_text, position=0)

    # Append the kept rounds verbatim
    for r in rounds_to_keep:
        new_round = ApiRound(
            started_at=r.started_at,
            token_estimate=0,  # invalidated; recounted below
        )
        for m in r.messages:
            new_round.append(m)
        new_log.rounds.append(new_round)

    # Optional: append an attachment-hint round so the model knows
    # what files exist in the working set even though the rounds that
    # touched them were summarized.
    recent_files = log.attachments.recent(n=10)
    if recent_files:
        hint_lines = [
            "[recently-touched files in this session]",
            *(f"  · {p}" for p in recent_files),
        ]
        hint_round = ApiRound(started_at=now_t)
        hint_round.append(LogMessage(
            role="system",
            content="\n".join(hint_lines),
            created_at=now_t,
        ))
        new_log.rounds.append(hint_round)

    # Refresh token estimates and finalize the boundary's post-compact count
    counter.refresh_round_estimates(new_log)
    post_tokens = counter.count_log(new_log)
    final_boundary = replace(boundary, post_compact_tokens=post_tokens)

    # The boundary message in the log holds older metadata; rebuild
    # the boundary marker round's first message to use the final stats.
    if new_log.rounds and new_log.rounds[0].messages:
        old_msg = new_log.rounds[0].messages[0]
        if old_msg.is_boundary:
            new_log.rounds[0].messages[0] = LogMessage(
                role="system",
                content=(
                    f"[compaction · {final_boundary.rounds_summarized} rounds · "
                    f"{final_boundary.pre_compact_tokens} → "
                    f"{final_boundary.post_compact_tokens} tokens]"
                ),
                created_at=final_boundary.happened_at,
                is_boundary=True,
            )

    return (new_log, final_boundary)


def _summarize_with_ptl_retry(
    *,
    log: MessageLog,
    rounds_to_summarize: list[ApiRound],
    client: CompactionClient,
    instructions: str,
    max_tokens: int,
) -> str:
    """Run the summarization, retrying on prompt-too-long by dropping
    the oldest rounds in chunks of PTL_DROP_PER_RETRY."""
    current_rounds = list(rounds_to_summarize)
    last_error: Exception | None = None

    for attempt in range(MAX_PTL_RETRIES + 1):
        if not current_rounds:
            raise CompactionError(
                "PTL retry exhausted — no rounds left to summarize"
            )

        prompt_messages = _build_summary_prompt(
            log=log,
            rounds_to_summarize=current_rounds,
            instructions=instructions,
        )

        try:
            stream = client.stream_chat(
                prompt_messages,
                max_tokens=max_tokens,
                temperature=SUMMARY_TEMPERATURE,
            )
            return _drain_to_summary(stream)
        except PromptTooLongError as exc:
            last_error = exc
            # Drop the oldest N rounds and retry
            drop_n = min(PTL_DROP_PER_RETRY, len(current_rounds))
            current_rounds = current_rounds[drop_n:]
            continue
        except CompactionError:
            raise

    raise CompactionError(
        f"PTL retry exhausted after {MAX_PTL_RETRIES} attempts: {last_error}"
    )
