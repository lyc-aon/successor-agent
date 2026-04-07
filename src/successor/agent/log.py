"""Message log shapes — designed compaction-ready from day one.

Why a custom log shape and not a flat list of dicts?

  - **Compaction needs to drop oldest rounds whole** (PTL recovery
    truncates by API-round granularity so the API never sees an
    orphaned tool_result without its tool_call).
  - **Compaction boundaries are first-class entities**, not
    overloaded into the message stream. The renderer needs to draw
    them as visible dividers; tests need to assert they exist.
  - **Token estimates are cached on each round** so the budget
    tracker doesn't have to re-tokenize on every loop iteration.
  - **Attachment registry tracks files mentioned by tool cards**
    so post-compact re-injection is deterministic.

Three types make up the log:

  - `LogMessage` — one user/assistant/tool/system message. Frozen.
  - `ApiRound` — one (user → assistant → tool_results) atomic unit.
                 The compactor drops these whole.
  - `MessageLog` — ordered list of rounds + boundaries + attachments.
                   Mutable container; the loop appends to it.

A "round" is the indivisible compaction unit. After a user message,
the assistant may emit text + tool calls; tool execution produces
results that are part of the SAME round (they exist only because
the user asked). The next round begins when the user speaks again
(or when the model needs to re-call after tool results — the
assistant message that follows tool results belongs to the SAME
round in our model, because dropping it would orphan the tool calls).

This shape mirrors free-code's `Message[]` semantics but with the
implicit "API round" boundaries made explicit, which simplifies
PTL truncation enormously.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Optional

from ..bash.cards import ToolCard


# ─── Roles ───
#
# We deliberately keep this small. The chat UI uses "user"/"successor"
# for display; for the API + log we normalize to OpenAI's vocabulary.

Role = Literal["system", "user", "assistant", "tool"]


# ─── A single message in the log ───


@dataclass(frozen=True, slots=True)
class LogMessage:
    """One message in the conversation log.

    Mostly a wrapper around (role, content) but carries enough metadata
    for compaction and rendering decisions:

    - `tool_card`: when set, this message is a tool execution result.
      The chat painter routes it to paint_tool_card. Compaction may
      replace the output with a placeholder via _replace_output().
    - `created_at`: monotonic timestamp for time-based microcompact.
    - `is_summary`: marker that this message is a compaction summary,
      not a real exchange. Excluded from token-cost-against-budget
      double-counting.
    - `is_boundary`: marker that this message is a compaction boundary
      header (system role) so the renderer can draw the divider line.
    """

    role: Role
    content: str
    tool_card: ToolCard | None = None
    created_at: float = 0.0
    is_summary: bool = False
    is_boundary: bool = False

    def to_api_dict(self) -> dict[str, str]:
        """Serialize to the OpenAI-compat shape llama.cpp expects.

        Tool cards become assistant messages with the raw command and
        the captured output appended (because llama.cpp doesn't know
        about our structured cards — it just sees text).

        Boundary markers and summary messages are emitted as USER
        messages with a clear `[compaction]` / `[summary]` prefix.
        We can't use role=system for them because Qwen3.5's chat
        template (and several other models') enforce "system message
        must be at the beginning" and reject any non-leading system
        messages with a Jinja exception. Wrapping the summary in a
        labeled user message preserves the information without
        violating the template's constraints — the model sees a
        clearly-marked context block from the harness rather than
        a real user turn.
        """
        if self.is_boundary:
            return {
                "role": "user",
                "content": f"[earlier conversation compacted] {self.content}",
            }
        if self.is_summary:
            return {
                "role": "user",
                "content": (
                    "[summary of earlier conversation, provided by the harness "
                    "after compaction — treat as authoritative context, not a "
                    "user turn]\n\n" + self.content
                ),
            }
        if self.tool_card is not None:
            card = self.tool_card
            body_lines = [f"$ {card.raw_command}"]
            if card.output:
                body_lines.append(card.output.rstrip())
            if card.stderr and card.stderr.strip():
                body_lines.append(f"[stderr] {card.stderr.rstrip()}")
            if card.exit_code is not None and card.exit_code != 0:
                body_lines.append(f"[exit {card.exit_code}]")
            return {"role": "assistant", "content": "\n".join(body_lines)}
        return {"role": self.role, "content": self.content}

    def replace_output(self, placeholder: str) -> "LogMessage":
        """Return a copy with the tool card's output replaced.

        Used by microcompact to clear stale tool result content while
        preserving the structural card so the chat history stays
        navigable. Returns self if there's no tool card.
        """
        if self.tool_card is None:
            return self
        from dataclasses import replace
        new_card = replace(
            self.tool_card,
            output=placeholder,
            stderr="",
            truncated=False,
        )
        return LogMessage(
            role=self.role,
            content=self.content,
            tool_card=new_card,
            created_at=self.created_at,
            is_summary=self.is_summary,
            is_boundary=self.is_boundary,
        )


# ─── A compaction boundary marker ───


@dataclass(frozen=True, slots=True)
class BoundaryMarker:
    """Metadata describing a compaction event.

    Attached to a synthetic system message in the log so the renderer
    can paint a visible divider line ("▔ summary of N turns · X→Y tokens ▔")
    that the user can scroll past to see what was preserved.
    """

    happened_at: float
    pre_compact_tokens: int
    post_compact_tokens: int
    rounds_summarized: int
    summary_text: str  # the actual summary the model produced
    reason: str = "auto"  # "auto" | "manual" | "reactive" (PTL recovery)

    @property
    def reduction_pct(self) -> float:
        """How much the compaction shrank the context, in percent."""
        if self.pre_compact_tokens == 0:
            return 0.0
        return 100.0 * (1.0 - self.post_compact_tokens / self.pre_compact_tokens)


# ─── An API round — the indivisible compaction unit ───


@dataclass(slots=True)
class ApiRound:
    """One coherent (user → assistant → optional tool results) unit.

    The compactor's PTL truncation drops these WHOLE — never half a
    round, because that would leave an orphaned tool_result without
    its tool_call (or vice versa) and the API would 400.

    Fields:
      messages       the messages in this round, in chronological order
      started_at     when the round began
      token_estimate cached token count (filled in by the loop after
                     each commit; budget tracker reads this without
                     re-tokenizing)
    """

    messages: list[LogMessage] = field(default_factory=list)
    started_at: float = 0.0
    token_estimate: int = 0

    def append(self, msg: LogMessage) -> None:
        self.messages.append(msg)
        # Token estimate gets refreshed by the caller via TokenCounter

    def text_for_tokenizing(self) -> str:
        """Concatenate everything in the round into a single string
        suitable for sending to llama.cpp's /tokenize endpoint."""
        return "\n".join(
            (m.tool_card.raw_command + "\n" + (m.tool_card.output or ""))
            if m.tool_card else m.content
            for m in self.messages
        )

    def char_count(self) -> int:
        return sum(len(m.content) + (
            len(m.tool_card.raw_command) + len(m.tool_card.output or "")
            if m.tool_card else 0
        ) for m in self.messages)

    @property
    def first_user_text(self) -> str:
        """The user message that started this round, for diagnostics."""
        for m in self.messages:
            if m.role == "user":
                return m.content
        return ""


# ─── The full message log ───


@dataclass
class AttachmentRegistry:
    """Tracks files seen by tool cards in the log.

    After compaction, these are re-injected so the model still knows
    what files exist in the working set. Stored as (path → last_seen)
    so we can re-attach the most recent ones first.
    """

    files: dict[str, float] = field(default_factory=dict)

    def note(self, path: str, *, at: float | None = None) -> None:
        self.files[path] = at if at is not None else time.monotonic()

    def recent(self, n: int = 10) -> list[str]:
        """Return the n most-recently-seen files."""
        return [
            p for p, _ in sorted(
                self.files.items(), key=lambda kv: kv[1], reverse=True
            )[:n]
        ]

    def __len__(self) -> int:
        return len(self.files)


@dataclass
class MessageLog:
    """The full conversation log — ordered rounds + boundaries + attachments.

    The chat owns one of these. The loop reads/writes it. Compaction
    rebuilds it. The renderer iterates it.

    Boundaries are stored AS rounds (single-message rounds containing
    a boundary message + a summary message), so the linear "rounds"
    list stays the only thing the renderer + compactor + token counter
    have to walk.
    """

    rounds: list[ApiRound] = field(default_factory=list)
    attachments: AttachmentRegistry = field(default_factory=AttachmentRegistry)
    # System prompt — never truncated, always sent first
    system_prompt: str = ""

    # ─── Mutation API ───

    def begin_round(self, *, started_at: float | None = None) -> ApiRound:
        """Start a new round and return it. Caller appends messages."""
        r = ApiRound(started_at=started_at or time.monotonic())
        self.rounds.append(r)
        return r

    def append_to_current_round(self, msg: LogMessage) -> None:
        """Append a message to the most recent round, creating one if empty."""
        if not self.rounds:
            self.begin_round()
        self.rounds[-1].append(msg)
        # Note attachments from tool cards
        if msg.tool_card is not None:
            for k, v in msg.tool_card.params:
                if k in ("path", "source", "destination") and v and v != "(missing)":
                    self.attachments.note(v)

    def insert_boundary(
        self,
        boundary: BoundaryMarker,
        summary_text: str,
        *,
        position: int | None = None,
    ) -> None:
        """Insert a compaction boundary at the given position (default: 0).

        Used by compact() to mark the start of the post-compact log.
        The boundary becomes a single-message round (system role,
        is_boundary=True) followed by another single-message round
        carrying the summary text.
        """
        bound_round = ApiRound(started_at=boundary.happened_at)
        bound_round.append(LogMessage(
            role="system",
            content=f"[compaction · {boundary.rounds_summarized} rounds · "
                    f"{boundary.pre_compact_tokens} → {boundary.post_compact_tokens} tokens]",
            created_at=boundary.happened_at,
            is_boundary=True,
        ))

        summary_round = ApiRound(started_at=boundary.happened_at)
        summary_round.append(LogMessage(
            role="system",
            content=summary_text,
            created_at=boundary.happened_at,
            is_summary=True,
        ))

        idx = 0 if position is None else position
        self.rounds.insert(idx, summary_round)
        self.rounds.insert(idx, bound_round)

    def truncate_oldest_round(self) -> ApiRound | None:
        """Remove and return the oldest round. Used by PTL retry."""
        if not self.rounds:
            return None
        return self.rounds.pop(0)

    # ─── Read API ───

    def total_messages(self) -> int:
        return sum(len(r.messages) for r in self.rounds)

    def total_token_estimate(self) -> int:
        """Sum the cached per-round token estimates.

        DOES NOT call the tokenizer. The loop is responsible for
        keeping the cached estimates fresh by calling
        TokenCounter.refresh_round_estimates() after each commit.
        """
        return sum(r.token_estimate for r in self.rounds)

    def boundaries(self) -> list[LogMessage]:
        """Return all boundary marker messages in the log."""
        return [
            m for r in self.rounds
            for m in r.messages
            if m.is_boundary
        ]

    def iter_messages(self) -> Iterator[LogMessage]:
        for r in self.rounds:
            yield from r.messages

    def api_messages(self) -> list[dict[str, str]]:
        """Build the OpenAI-compat message list to send to llama.cpp.

        Always starts with the system prompt; then walks rounds in
        order. Boundary messages render as system messages so the
        model gets context about what was summarized.
        """
        out: list[dict[str, str]] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        for r in self.rounds:
            for m in r.messages:
                out.append(m.to_api_dict())
        return out

    def is_empty(self) -> bool:
        return not any(r.messages for r in self.rounds)

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    @property
    def latest_round(self) -> ApiRound | None:
        return self.rounds[-1] if self.rounds else None
