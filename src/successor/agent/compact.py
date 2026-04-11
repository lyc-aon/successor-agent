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
# to fire. Compaction needs one older round to summarize and one
# newer round to preserve verbatim, so two rounds is the true floor.
MIN_ROUNDS_TO_COMPACT: int = 2

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
    rounds_in_send: list[ApiRound],
    keep_recent_rounds: int,
    instructions: str,
) -> list[dict[str, str]]:
    """Build a KV-cache-friendly summarization prompt.

    **The key insight**: send the existing chat structure (system
    prompt + all rounds in `rounds_in_send`) followed by a single
    user instruction message asking for a summary. This makes the
    prompt's prefix MATCH WHAT THE CHAT HAS ALREADY SENT during
    normal conversation, so llama.cpp can reuse its KV cache for
    everything but the trailing instruction.

    Cost comparison at 256K context:
      Old fresh-prompt approach: ~14 minutes prompt eval (cache miss)
      Cache-friendly approach:   ~1 second prompt eval (full reuse)
                                 + the actual summary generation cost

    The model sees the entire conversation including the rounds
    we're going to keep verbatim. The instruction tells it to focus
    its summary on the OLDER portion (everything before the kept
    rounds). This produces some redundancy but the cache savings
    are massive and the redundancy is harmless because the kept
    rounds are preserved in the post-compact log anyway.

    `rounds_in_send` is the rounds list we're actually sending. In
    the happy path this is `log.rounds`. The PTL retry path may
    pass a subset (oldest rounds dropped) when the full log is
    too large for the model's context window.
    """
    # Start with the chat's normal serialization — system prompt
    # plus every round in rounds_in_send. This is the prefix that
    # already lives in the KV cache.
    messages: list[dict[str, str]] = []
    if log.system_prompt:
        messages.append({"role": "system", "content": log.system_prompt})
    for r in rounds_in_send:
        for m in r.messages:
            messages.append(m.to_api_dict())

    # Append the summarization instruction as a user message.
    # The model interprets this as a continuation of the chat where
    # the user asks "now please summarize everything above".
    n_keep = max(0, keep_recent_rounds)
    keep_phrase = (
        f"the most recent {n_keep} turn{'s' if n_keep != 1 else ''}"
        if n_keep > 0
        else "no turns (summarize everything)"
    )
    messages.append({
        "role": "user",
        "content": (
            "[harness instruction — please follow exactly, do not "
            "address me as the user, do not begin with a greeting]\n\n"
            + instructions
            + "\n\nProduce a summary of every turn of this conversation "
            + f"EXCEPT {keep_phrase}, which the harness is preserving "
            + "verbatim. Begin the summary directly — no preface, "
            + "no \"Here is the summary\", no first-person address."
        ),
    })
    return messages


def normalized_keep_recent_rounds(
    round_count: int,
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
) -> int:
    """Return a keep_recent value that leaves something to summarize.

    The compactor's real structural requirement is simple:
      - keep at least one recent round verbatim
      - summarize at least one older round

    Older code enforced an arbitrary 4-round minimum, which blocked
    huge two-round conversations from compacting at all. This helper
    encodes the actual invariant instead.
    """
    if round_count <= 1:
        return 1
    if keep_recent_rounds <= 0:
        keep_recent_rounds = 1
    if keep_recent_rounds >= round_count:
        keep_recent_rounds = max(1, round_count // 2)
    return min(keep_recent_rounds, round_count - 1)


def can_compact_log(
    log: MessageLog,
    *,
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
) -> bool:
    """Whether this log can be compacted meaningfully.

    A log is compactable when there is at least one older round to
    summarize and at least one recent round to keep. Token mass is
    handled elsewhere by the budget gate; this helper answers only
    the structural question.
    """
    if len(log.rounds) < MIN_ROUNDS_TO_COMPACT:
        return False
    keep_recent = normalized_keep_recent_rounds(
        len(log.rounds),
        keep_recent_rounds=keep_recent_rounds,
    )
    return (len(log.rounds) - keep_recent) >= 1


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
    if not can_compact_log(log, keep_recent_rounds=keep_recent_rounds):
        raise ValueError(
            "need at least 2 rounds to compact "
            "(one older round to summarize and one recent round to keep), "
            f"got {len(log.rounds)}"
        )

    # Pre-compact token count for the boundary marker
    pre_tokens = counter.count_log(log)

    # Split the log: rounds to summarize vs rounds to keep
    keep_recent_rounds = normalized_keep_recent_rounds(
        len(log.rounds),
        keep_recent_rounds=keep_recent_rounds,
    )

    rounds_to_summarize = log.rounds[: -keep_recent_rounds]
    rounds_to_keep = log.rounds[-keep_recent_rounds:]

    if not rounds_to_summarize:
        raise ValueError(
            f"after splitting, no rounds to summarize "
            f"(rounds={len(log.rounds)}, keep={keep_recent_rounds})"
        )

    # ─── Run the summarization with PTL retry ───
    # Cache-friendly path: sends the full log + an instruction message
    # so llama.cpp can reuse its KV cache for the entire prefix.
    summary_text = _summarize_with_ptl_retry(
        log=log,
        keep_recent_rounds=keep_recent_rounds,
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

    # ─── Post-compact size assertion ───
    #
    # If the new log is >= 90% of the original size, the compaction
    # failed to shrink anything meaningful. The most common causes are:
    #   1. The model produced a verbose summary (large summary_max_tokens
    #      + a chatty model)
    #   2. keep_recent_rounds was too large for the log size — most
    #      rounds got preserved verbatim, only a tiny prefix summarized
    #   3. The log was already short and mostly recent rounds
    #
    # We do NOT raise — the new log is still the right thing to use,
    # it's just less effective than expected. We attach a `warning`
    # string to the boundary marker so the chat can surface it (and
    # so the recompact-chain detector in BudgetTracker has visible
    # signal to act on).
    warning = ""
    if pre_tokens > 0 and post_tokens >= pre_tokens * 0.9:
        reduction = 100.0 * (1.0 - post_tokens / pre_tokens)
        warning = (
            f"compaction underperformed: {pre_tokens} → {post_tokens} "
            f"tokens (only {reduction:.1f}% reduction). The summary may "
            f"be too long, or keep_recent_rounds may be too large."
        )

    final_boundary = replace(
        boundary,
        post_compact_tokens=post_tokens,
        warning=warning,
    )

    # The boundary message in the log holds older metadata; rebuild
    # the boundary marker round's first message to use the final stats.
    if new_log.rounds and new_log.rounds[0].messages:
        old_msg = new_log.rounds[0].messages[0]
        if old_msg.is_boundary:
            content = (
                f"[compaction · {final_boundary.rounds_summarized} rounds · "
                f"{final_boundary.pre_compact_tokens} → "
                f"{final_boundary.post_compact_tokens} tokens]"
            )
            if warning:
                content += " ⚠ underperformed"
            new_log.rounds[0].messages[0] = LogMessage(
                role="system",
                content=content,
                created_at=final_boundary.happened_at,
                is_boundary=True,
            )

    return (new_log, final_boundary)


def _summarize_with_ptl_retry(
    *,
    log: MessageLog,
    keep_recent_rounds: int,
    client: CompactionClient,
    instructions: str,
    max_tokens: int,
) -> str:
    """Run the cache-friendly summarization with PTL retry.

    First attempt sends the FULL log (all rounds) so the chat's
    KV cache prefix matches and prompt eval is essentially free.
    On prompt-too-long, drop the oldest PTL_DROP_PER_RETRY rounds
    and retry. The cache match shrinks with each retry but the
    summarization is still cache-friendly for whatever rounds
    remain.

    Note: PTL retry is unlikely to fire in practice when the chat
    has been working with the same context window — if the chat's
    normal sends fit, then [chat_send + small_instruction] also
    fits unless we're right at the edge. The retry is here as a
    failsafe for the edge case.
    """
    current_rounds = list(log.rounds)
    last_error: Exception | None = None

    for attempt in range(MAX_PTL_RETRIES + 1):
        if not current_rounds:
            raise CompactionError(
                "PTL retry exhausted — no rounds left to summarize"
            )

        prompt_messages = _build_summary_prompt(
            log=log,
            rounds_in_send=current_rounds,
            keep_recent_rounds=keep_recent_rounds,
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
            # Drop the oldest N rounds and retry. We drop from the
            # FULL log this time (not just rounds_to_summarize) so
            # the next attempt has fewer total tokens.
            drop_n = min(PTL_DROP_PER_RETRY, len(current_rounds))
            current_rounds = current_rounds[drop_n:]
            continue
        except CompactionError:
            raise

    raise CompactionError(
        f"PTL retry exhausted after {MAX_PTL_RETRIES} attempts: {last_error}"
    )
