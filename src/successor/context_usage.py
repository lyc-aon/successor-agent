"""Canonical request-envelope accounting for chat context usage.

This module gives Successor one shared source of truth for:

- the exact next outbound request envelope the chat plans to send
- a provider-aware token estimate for that envelope
- normalized provider usage telemetry from completed streams
- a cached snapshot shape that the footer, /budget, and autocompact
  gate can all consume consistently

Exact cross-provider token accounting is not possible without each
provider exposing both the real tokenizer and the real chat template.
The approach here is therefore:

1. Count the actual request components we know about deterministically
   (system prompt sections, serialized messages, tool schemas).
2. Use an exact provider tokenizer when one exists (llama.cpp), and a
   conservative heuristic otherwise.
3. Calibrate the estimate toward provider-reported input token usage
   whenever a completed stream returns real usage fields.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .agent.tokens import HEURISTIC_CHARS_PER_TOKEN, TokenCounter


@dataclass(frozen=True, slots=True)
class PromptSection:
    key: str
    label: str
    content: str
    cache_break: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TurnRequestEnvelope:
    turn: int
    system_sections: tuple[PromptSection, ...]
    system_prompt: str
    api_messages: tuple[dict[str, Any], ...]
    request_messages: tuple[dict[str, Any], ...]
    tool_schemas: tuple[dict[str, Any], ...]
    enabled_tools: tuple[str, ...]
    enabled_skills: tuple[str, ...]
    volatile_sections: tuple[PromptSection, ...] = ()
    continuation_prefill_applied: bool = False
    volatile_tail_applied: bool = False
    browser_verification_active: bool = False
    browser_verification_reason: str = ""
    last_user_excerpt: str = ""
    stable_system_hash: str = ""
    volatile_tail_hash: str = ""
    cache_break_reasons: tuple[str, ...] = ()
    request_slot_id: int | None = None
    request_cache_prompt: bool | None = None


@dataclass(frozen=True, slots=True)
class UsageBreakdownEntry:
    key: str
    label: str
    tokens: int


@dataclass(frozen=True, slots=True)
class RequestTokenEstimate:
    input_tokens: int
    input_tokens_raw: int
    method: str
    confidence: str
    calibration_factor: float
    breakdown: tuple[UsageBreakdownEntry, ...]


@dataclass(frozen=True, slots=True)
class UsageTelemetry:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None
    source: str
    raw_usage: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ContextUsageSnapshot:
    source: str
    turn: int
    used_tokens: int
    input_tokens: int
    output_tokens: int
    window: int
    warning_at: int
    autocompact_at: int
    blocking_at: int
    fill_pct: float
    headroom: int
    state: str
    method: str
    confidence: str
    calibration_factor: float
    breakdown: tuple[UsageBreakdownEntry, ...]
    last_actual_usage: UsageTelemetry | None = None


def join_prompt_sections(sections: list[PromptSection] | tuple[PromptSection, ...]) -> str:
    return "\n\n".join(
        section.content for section in sections if section.content.strip()
    ).strip()


def normalize_usage_payload(raw_usage: dict[str, Any] | None) -> UsageTelemetry | None:
    if not isinstance(raw_usage, dict):
        return None

    input_tokens = _first_int(
        raw_usage,
        "input_tokens",
        "prompt_tokens",
        "prompt_eval_count",
        "prompt_token_count",
    )
    output_tokens = _first_int(
        raw_usage,
        "output_tokens",
        "completion_tokens",
        "completion_eval_count",
        "generated_tokens",
    )
    total_tokens = _first_int(
        raw_usage,
        "total_tokens",
        "total_token_count",
    )
    reasoning_tokens = _first_nested_int(
        raw_usage,
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    )

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if output_tokens is None and total_tokens is not None and input_tokens is not None:
        output_tokens = max(0, total_tokens - input_tokens)
    if input_tokens is None and total_tokens is not None and output_tokens is not None:
        input_tokens = max(0, total_tokens - output_tokens)

    if (
        input_tokens is None
        and output_tokens is None
        and total_tokens is None
        and reasoning_tokens is None
    ):
        return None

    return UsageTelemetry(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        source="provider_usage",
        raw_usage=raw_usage,
    )


def update_calibration_factor(
    current_factor: float,
    *,
    estimated_input_tokens_raw: int,
    actual_input_tokens: int | None,
) -> float:
    if estimated_input_tokens_raw <= 0 or actual_input_tokens is None or actual_input_tokens <= 0:
        return current_factor
    observed = actual_input_tokens / max(1, estimated_input_tokens_raw)
    observed = max(0.5, min(2.0, observed))
    baseline = current_factor if current_factor > 0 else 1.0
    return max(0.5, min(2.0, baseline + (observed - baseline) * 0.25))


def estimate_live_output_tokens(*, reasoning_chars: int = 0, content_text: str = "") -> int:
    total_chars = max(0, reasoning_chars) + max(0, len(content_text or ""))
    if total_chars <= 0:
        return 0
    return max(1, math.ceil(total_chars / HEURISTIC_CHARS_PER_TOKEN))


def estimate_request_input_tokens(
    envelope: TurnRequestEnvelope,
    counter: TokenCounter,
    *,
    calibration_factor: float = 1.0,
) -> RequestTokenEstimate:
    raw_entries: list[UsageBreakdownEntry] = []
    message_framing_tokens = 0

    for section in envelope.system_sections:
        if not section.content:
            continue
        raw_entries.append(
            UsageBreakdownEntry(
                key=f"system:{section.key}",
                label=f"system · {section.label}",
                tokens=counter.count(section.content),
            )
        )

    for idx, msg in enumerate(envelope.request_messages):
        role = str(msg.get("role") or "")
        if role == "system":
            message_framing_tokens += 4
            continue

        message_framing_tokens += 4
        content = str(msg.get("content") or "")
        if role == "user":
            if envelope.continuation_prefill_applied and idx == len(envelope.request_messages) - 1:
                _append_tokens(
                    raw_entries,
                    key="request:continuation_guard",
                    label="request · continuation guard",
                    tokens=counter.count(content),
                )
            else:
                _append_tokens(
                    raw_entries,
                    key="conversation:user",
                    label="conversation · user",
                    tokens=counter.count(content),
                )
        elif role == "assistant":
            _append_tokens(
                raw_entries,
                key="conversation:assistant",
                label="conversation · assistant",
                tokens=counter.count(content),
            )
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                _append_tokens(
                    raw_entries,
                    key="conversation:assistant_tool_calls",
                    label="conversation · assistant tool calls",
                    tokens=_count_json(counter, tool_calls),
                )
        elif role == "tool":
            _append_tokens(
                raw_entries,
                key="conversation:tool_results",
                label="conversation · tool results",
                tokens=counter.count(content),
            )
        else:
            _append_tokens(
                raw_entries,
                key=f"conversation:{role or 'unknown'}",
                label=f"conversation · {role or 'unknown'}",
                tokens=counter.count(content),
            )

        name = str(msg.get("name") or "")
        if name:
            _append_tokens(
                raw_entries,
                key="conversation:message_metadata",
                label="conversation · message metadata",
                tokens=counter.count(name),
            )
        tool_call_id = str(msg.get("tool_call_id") or "")
        if tool_call_id:
            _append_tokens(
                raw_entries,
                key="conversation:message_metadata",
                label="conversation · message metadata",
                tokens=counter.count(tool_call_id),
            )

    if envelope.tool_schemas:
        _append_tokens(
            raw_entries,
            key="request:tool_schemas",
            label="request · tool schemas",
            tokens=_count_json(counter, list(envelope.tool_schemas)),
        )

    if message_framing_tokens:
        raw_entries.append(
            UsageBreakdownEntry(
                key="request:message_framing",
                label="request · message framing",
                tokens=message_framing_tokens,
            )
        )

    raw_total = sum(entry.tokens for entry in raw_entries)
    scaled_total = (
        0 if raw_total <= 0 else max(1, math.ceil(raw_total * max(0.5, calibration_factor)))
    )
    scaled_entries = _scale_breakdown(raw_entries, target_total=scaled_total)

    method = counter.counting_method()
    if method == "heuristic":
        confidence = "medium" if abs(calibration_factor - 1.0) >= 0.05 else "low"
    else:
        confidence = "high"

    return RequestTokenEstimate(
        input_tokens=scaled_total,
        input_tokens_raw=raw_total,
        method=method,
        confidence=confidence,
        calibration_factor=calibration_factor,
        breakdown=tuple(scaled_entries),
    )


def build_context_usage_snapshot(
    estimate: RequestTokenEstimate,
    *,
    budget: Any,
    turn: int,
    source: str,
    output_tokens: int = 0,
    last_actual_usage: UsageTelemetry | None = None,
) -> ContextUsageSnapshot:
    used_tokens = max(0, estimate.input_tokens + max(0, output_tokens))
    return ContextUsageSnapshot(
        source=source,
        turn=turn,
        used_tokens=used_tokens,
        input_tokens=estimate.input_tokens,
        output_tokens=max(0, output_tokens),
        window=budget.window,
        warning_at=budget.warning_at,
        autocompact_at=budget.autocompact_at,
        blocking_at=budget.blocking_at,
        fill_pct=budget.fill_pct(used_tokens),
        headroom=budget.headroom(used_tokens),
        state=budget.state(used_tokens),
        method=estimate.method,
        confidence=estimate.confidence,
        calibration_factor=estimate.calibration_factor,
        breakdown=estimate.breakdown,
        last_actual_usage=last_actual_usage,
    )


def _append_tokens(
    entries: list[UsageBreakdownEntry],
    *,
    key: str,
    label: str,
    tokens: int,
) -> None:
    if tokens <= 0:
        return
    for idx, entry in enumerate(entries):
        if entry.key != key:
            continue
        entries[idx] = UsageBreakdownEntry(
            key=entry.key,
            label=entry.label,
            tokens=entry.tokens + tokens,
        )
        return
    entries.append(UsageBreakdownEntry(key=key, label=label, tokens=tokens))


def _count_json(counter: TokenCounter, payload: Any) -> int:
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return counter.count(serialized)


def _scale_breakdown(
    entries: list[UsageBreakdownEntry],
    *,
    target_total: int,
) -> list[UsageBreakdownEntry]:
    if not entries:
        return []
    raw_total = sum(entry.tokens for entry in entries)
    if raw_total <= 0 or raw_total == target_total:
        return list(entries)

    floors: list[int] = []
    fractions: list[tuple[float, int]] = []
    for idx, entry in enumerate(entries):
        scaled = (entry.tokens / raw_total) * target_total
        floor_value = int(math.floor(scaled))
        floors.append(floor_value)
        fractions.append((scaled - floor_value, idx))

    remainder = max(0, target_total - sum(floors))
    for _, idx in sorted(fractions, reverse=True):
        if remainder <= 0:
            break
        floors[idx] += 1
        remainder -= 1

    return [
        UsageBreakdownEntry(
            key=entry.key,
            label=entry.label,
            tokens=floors[idx],
        )
        for idx, entry in enumerate(entries)
        if floors[idx] > 0
    ]


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _first_nested_int(
    payload: dict[str, Any],
    *paths: tuple[str, ...],
) -> int | None:
    for path in paths:
        node: Any = payload
        valid = True
        for segment in path:
            if not isinstance(node, dict):
                valid = False
                break
            node = node.get(segment)
        if valid and isinstance(node, int) and node >= 0:
            return node
    return None
