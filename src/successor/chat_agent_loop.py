"""Agent-loop/controller orchestration extracted from SuccessorChat.

This module keeps the current chat behavior intact while pulling the
submit/turn/stream controller layer out of `chat.py`. The chat remains
the owner of state, rendering, and trace sinks; this helper owns the
core agent loop behavior and the API-history assembly helpers it uses.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .agent.bash_stream import BashStreamDetector
from .agent.loop import (
    MAX_TRANSIENT_RETRIES,
    TRANSIENT_BACKOFF_BASE_S,
    is_transient_stream_error,
)
from .bash import ToolCard, resolve_bash_config
from .context_usage import (
    PromptSection,
    TurnRequestEnvelope,
    join_prompt_sections,
)
from .file_tools import note_non_read_tool_call
from .profiles import DEFAULT_MAX_AGENT_TURNS, PROFILE_REGISTRY, get_profile
from .providers.llama import (
    ContentChunk,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)
from .render.theme import all_themes, get_theme
from .runbook import (
    build_runbook_execution_primer,
    build_runbook_execution_guidance,
    build_runbook_prompt_section,
)
from .session_trace import clip_text as _trace_clip_text
from .skills import (
    build_skill_discovery_section,
    build_skill_hint_section,
)
from .subagents.cards import SubagentToolCard
from .task_adoption import maybe_build_task_adoption_nudge
from .tasks import (
    build_task_continue_nudge,
    build_task_execution_primer,
    build_task_execution_guidance,
    build_task_prompt_section,
)
from .tools_registry import (
    build_model_tool_guidance,
    build_native_tool_schemas,
)
from .verification_contract import (
    build_verification_continue_nudge,
    build_verification_execution_primer,
    build_verification_execution_guidance,
    build_verification_prompt_section,
)
from .verification_adoption import maybe_build_verification_adoption_nudge
from .verification_hints import build_repo_verification_guidance
from .web.verification import build_browser_verification_guidance

ToolArtifact = ToolCard | SubagentToolCard
_INTERNAL_CONTINUATION_PREFILL = (
    "[internal harness continuation]\n"
    "Continue from the current conversation state and follow the latest "
    "system reminders. This is not a new user request."
)
_INTERNAL_RUNTIME_CONTEXT_PREFIX = (
    "[internal harness runtime context]\n"
    "This is harness-supplied working context for the current turn, not "
    "a new user request."
)
_STABLE_SECTION_REASON_LABELS = {
    "base": "system prompt",
    "working_directory": "working directory guidance",
    "repo_verification": "repository verification guidance",
    "execution_discipline": "execution discipline",
    "task_primer": "task-ledger primer",
    "verification_primer": "verification primer",
    "runbook_primer": "runbook primer",
    "parallel_tool_calls": "parallel tool-call guidance",
    "tool_guidance": "tool guidance",
    "skill_hints": "skill hints",
    "skill_discovery": "skill discovery",
}


@dataclass(slots=True)
class _PreparedTurnRequest:
    envelope: TurnRequestEnvelope
    task_adoption_decision: Any
    stable_prompt_key: tuple[tuple[str, str], ...]
    stable_prompt_cache_hit: bool


def _message_has_tool_artifact(msg: Any) -> bool:
    return msg.tool_card is not None or msg.subagent_card is not None


def _message_tool_artifact(msg: Any) -> ToolArtifact | None:
    if msg.tool_card is not None:
        return msg.tool_card
    return msg.subagent_card


def _api_role_for_message(msg: Any) -> str:
    if msg.api_role_override:
        return msg.api_role_override
    if msg.role == "successor":
        return "assistant"
    return msg.role


def _tool_name_for_card(card: ToolArtifact) -> str:
    return "subagent" if isinstance(card, SubagentToolCard) else card.tool_name


def _tool_arguments_for_card(card: ToolArtifact) -> dict[str, Any]:
    if isinstance(card, SubagentToolCard):
        payload = {"prompt": card.directive}
        if card.name:
            payload["name"] = card.name
        if card.role:
            payload["role"] = card.role
        return payload
    if card.tool_arguments:
        return dict(card.tool_arguments)
    return {"command": card.raw_command}


def _assistant_with_tool_calls(content: str, cards: list[ToolArtifact]) -> dict:
    """Build the assistant message dict for a tool-calling turn."""
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": card.tool_call_id,
                "type": "function",
                "function": {
                    "name": _tool_name_for_card(card),
                    "arguments": _canonical_json(_tool_arguments_for_card(card)),
                },
            }
            for card in cards
        ],
    }


def _tool_card_content_for_api(card: ToolArtifact) -> str:
    """Build the `role=tool` content that goes back to the model."""
    if isinstance(card, SubagentToolCard):
        return card.spawn_result
    if card.api_content_override is not None:
        return card.api_content_override

    parts: list[str] = []
    if card.output:
        parts.append(card.output.rstrip())
    if card.stderr and card.stderr.strip():
        parts.append(card.stderr.rstrip())
    if card.exit_code is not None and card.exit_code != 0:
        parts.append(f"[command exited with code {card.exit_code}]")
    return "\n".join(parts)


def _find_last_user_excerpt(api_messages: list[dict[str, Any]]) -> str:
    for msg in reversed(api_messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return _trace_clip_text(content, limit=320)
    return ""


def _hash_fragments(*parts: str) -> str:
    digest = hashlib.sha1()
    for part in parts:
        if not part:
            continue
        digest.update(part.encode("utf-8", errors="replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:12]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _format_runtime_tail_context(sections: list[PromptSection]) -> str:
    body = join_prompt_sections(sections)
    if not body:
        return ""
    return f"{_INTERNAL_RUNTIME_CONTEXT_PREFIX}\n\n{body}"


def _append_runtime_tail_context(
    api_messages: list[dict[str, Any]],
    context_text: str,
) -> tuple[list[dict[str, Any]], bool]:
    if not context_text.strip():
        return api_messages, False
    merged = list(api_messages)
    # Keep runtime context as its own synthetic user turn. Merging it
    # into an existing user message rewrites older prompt bytes and
    # limits prefix reuse on cache-friendly providers.
    merged.append({"role": "user", "content": context_text})
    return merged, True


def _append_api_message(
    api_messages: list[dict[str, Any]],
    *,
    role: str,
    content: str,
) -> None:
    if not content:
        return
    # Preserve message boundaries exactly. Collapsing adjacent
    # same-role messages reduces framing tokens a bit, but it rewrites
    # older message bytes when a new message arrives and fights KV
    # cache reuse.
    api_messages.append({"role": role, "content": content})


def _hash_message_sequence(
    messages: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    tool_schemas: tuple[dict[str, Any], ...] = (),
) -> str:
    return _hash_fragments(
        _canonical_json(list(messages)),
        _canonical_json(tool_schemas),
    )


def _append_internal_continuation_prefill(
    api_messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Append a transient user turn when an internal continuation would
    otherwise end on an assistant message.

    llama.cpp rejects assistant-ended prompts in thinking mode
    ("Assistant response prefill is incompatible with enable_thinking").
    Internal continuation turns are the main way Successor can re-enter
    the loop without a fresh user message, so guard only that case and
    keep the transcript itself unchanged.
    """
    if not api_messages or api_messages[-1].get("role") != "assistant":
        return api_messages, False
    guarded = list(api_messages)
    guarded.append({"role": "user", "content": _INTERNAL_CONTINUATION_PREFILL})
    return guarded, True


def _trace_tool_call_summary(tc: dict[str, Any]) -> dict[str, object]:
    name = str(tc.get("name") or "")
    args = tc.get("arguments") or {}
    entry: dict[str, object] = {
        "id": str(tc.get("id") or ""),
        "name": name,
    }
    raw_arguments = str(tc.get("raw_arguments") or "")
    parse_error = str(tc.get("arguments_parse_error") or "").strip()
    parse_error_pos = tc.get("arguments_parse_error_pos")
    if raw_arguments:
        entry["raw_arguments_len"] = len(raw_arguments)
    if parse_error:
        entry["arguments_parse_error"] = parse_error
        if isinstance(parse_error_pos, int):
            entry["arguments_parse_error_pos"] = parse_error_pos
    if name == "bash" and isinstance(args, dict):
        entry["command_excerpt"] = _trace_clip_text(
            str(args.get("command") or ""),
            limit=320,
        )
    elif name == "bash" and raw_arguments:
        entry["raw_arguments_excerpt"] = _trace_clip_text(raw_arguments, limit=320)
    elif name == "task" and isinstance(args, dict):
        items = args.get("items")
        if isinstance(items, list):
            entry["task_count"] = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "").strip().lower() != "in_progress":
                    continue
                entry["active_task"] = _trace_clip_text(
                    str(item.get("active_form") or item.get("content") or ""),
                    limit=320,
                )
                break
    elif name == "verify" and isinstance(args, dict):
        items = args.get("items")
        if isinstance(items, list):
            entry["assertion_count"] = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "").strip().lower() != "in_progress":
                    continue
                entry["active_claim"] = _trace_clip_text(
                    str(item.get("claim") or ""),
                    limit=320,
                )
                entry["active_evidence"] = _trace_clip_text(
                    str(item.get("evidence") or ""),
                    limit=320,
                )
                break
    elif name == "runbook" and isinstance(args, dict):
        if bool(args.get("clear")):
            entry["cleared"] = True
        objective = str(args.get("objective") or "").strip()
        if objective:
            entry["objective_excerpt"] = _trace_clip_text(objective, limit=320)
        hypothesis = str(args.get("active_hypothesis") or "").strip()
        if hypothesis:
            entry["active_hypothesis"] = _trace_clip_text(hypothesis, limit=320)
        status = str(args.get("status") or "").strip()
        if status:
            entry["status"] = status
        baseline_status = str(args.get("baseline_status") or "").strip()
        if baseline_status:
            entry["baseline_status"] = baseline_status
        evaluator = args.get("evaluator")
        if isinstance(evaluator, list):
            entry["evaluator_count"] = len(evaluator)
        attempt = args.get("attempt")
        if isinstance(attempt, dict):
            entry["attempt_decision"] = str(attempt.get("decision") or "")
            attempt_hypothesis = str(attempt.get("hypothesis") or "").strip()
            if attempt_hypothesis:
                entry["attempt_hypothesis"] = _trace_clip_text(
                    attempt_hypothesis,
                    limit=320,
                )
    elif name == "skill" and isinstance(args, dict):
        entry["skill_name"] = str(args.get("skill") or "")
        task = str(args.get("task") or "")
        if task:
            entry["task_excerpt"] = _trace_clip_text(task, limit=320)
    elif name == "subagent" and isinstance(args, dict):
        entry["prompt_excerpt"] = _trace_clip_text(
            str(args.get("prompt") or ""),
            limit=320,
        )
        label = str(args.get("name") or "")
        if label:
            entry["task_name"] = label
        role = str(args.get("role") or "").strip()
        if role:
            entry["role"] = role
    elif name == "holonet" and isinstance(args, dict):
        entry["provider"] = str(args.get("provider") or "")
        entry["query_excerpt"] = _trace_clip_text(
            str(args.get("query") or args.get("url") or ""),
            limit=320,
        )
    elif name == "browser" and isinstance(args, dict):
        entry["action"] = str(args.get("action") or "")
        entry["target_excerpt"] = _trace_clip_text(
            str(args.get("target") or args.get("url") or ""),
            limit=320,
        )
    elif args:
        entry["arguments_excerpt"] = _trace_clip_text(str(args), limit=320)
    return entry


class ChatAgentLoop:
    """Owns the agent-loop controller flow while the chat owns state."""

    def __init__(
        self,
        host: Any,
        message_cls: type[Any],
        *,
        densities: tuple[Any, ...],
        find_density: Callable[[str], Any | None],
        max_agent_turns_default: int = DEFAULT_MAX_AGENT_TURNS,
    ) -> None:
        self._host = host
        self._message_cls = message_cls
        self._densities = densities
        self._find_density = find_density
        self._max_agent_turns_default = max_agent_turns_default

    def _message(self, role: str, raw_text: str, **kwargs: Any) -> Any:
        return self._message_cls(role, raw_text, **kwargs)

    def _append(self, message: Any) -> None:
        self._host.messages.append(message)

    def submit(self) -> None:
        text = self._host.input_buffer.strip()
        if text:
            self._host._trace_event(
                "user_submit",
                text=text,
                excerpt=_trace_clip_text(text, limit=320),
            )
        if not text.startswith("/history"):
            self._host._history_add(text)
        self._host.input_buffer = ""

        if self._host._cache_warmer is not None:
            self._host._cache_warmer.close()
            self._host._cache_warmer = None

        if self._host._running_tools:
            self._host._cancel_running_tools()
            self._host._pending_continuation = False
        note_non_read_tool_call(self._host._file_read_tracker)

        if text in ("/quit", "/exit", "/q"):
            self._host.stop()
            return

        if text == "/config":
            if self._host._has_active_subagent_tasks():
                self._append(
                    self._message(
                        "successor",
                        "wait for background subagent tasks to finish before opening /config.",
                        synthetic=True,
                    )
                )
                return
            self._host._pending_action = "config"
            self._host.stop()
            return

        if text.startswith("/fork"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self._append(
                    self._message(
                        "successor",
                        "usage: /fork <directive>. Spawns a background subagent that inherits the current chat context.",
                        synthetic=True,
                    )
                )
                return
            self._host._handle_fork_cmd(parts[1].strip())
            return

        if text == "/tasks":
            self._host._handle_tasks_cmd()
            return

        if text.startswith("/task-cancel"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self._append(
                    self._message(
                        "successor",
                        "usage: /task-cancel <task-id|all>",
                        synthetic=True,
                    )
                )
                return
            self._host._handle_task_cancel_cmd(parts[1].strip())
            return

        if text.startswith("/bash"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self._append(
                    self._message(
                        "successor",
                        "usage: /bash <command>. The command runs locally and "
                        "renders as a structured tool card. Dangerous commands "
                        "(rm -rf /, sudo, curl|sh, etc.) are refused with an "
                        "explanation.",
                        synthetic=True,
                    )
                )
                return
            command = parts[1].strip()
            self._append(self._message("user", f"`{command}`", synthetic=True))
            bash_cfg = resolve_bash_config(self._host.profile)
            self._host._spawn_bash_runner(command, bash_cfg=bash_cfg)
            return

        if text == "/budget":
            self._host._handle_budget_cmd()
            return

        if text in ("/perf", "/kv"):
            self._host._handle_perf_cmd()
            return

        if text.startswith("/history"):
            parts = text.split(maxsplit=1)
            initial_query = parts[1].strip() if len(parts) > 1 else ""
            self._host._open_history_overlay(initial_query=initial_query)
            return

        if text.startswith("/burn"):
            parts = text.split()
            if len(parts) < 2:
                self._append(
                    self._message(
                        "successor",
                        "usage: /burn <N>  → inject N synthetic tokens of "
                        "varied content into the chat history. Use this to "
                        "stress-test compaction without burning real model "
                        "calls. Pair with /budget to watch the fill % climb "
                        "and /compact to fire the summarizer.",
                        synthetic=True,
                    )
                )
                return
            try:
                n_tokens = int(parts[1])
            except ValueError:
                self._append(
                    self._message(
                        "successor",
                        f"unknown /burn argument '{parts[1]}'. Expected an integer token count.",
                        synthetic=True,
                    )
                )
                return
            self._host._handle_burn_cmd(n_tokens)
            return

        if text == "/compact":
            self._host._handle_compact_cmd()
            return

        if text.startswith("/profile"):
            parts = text.split(maxsplit=1)
            available_names = sorted(PROFILE_REGISTRY.names())
            if len(parts) == 1:
                names = ", ".join(available_names) or "(none loaded)"
                hint = (
                    f"current profile: {self._host.profile.name}"
                    + (
                        f" — {self._host.profile.description}"
                        if self._host.profile.description else ""
                    )
                    + f". Available: {names}. "
                    f"Use /profile <name> or Ctrl+P to cycle."
                )
                self._append(self._message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._host._cycle_profile()
                return
            target = get_profile(arg)
            if target is None:
                self._append(
                    self._message(
                        "successor",
                        f"no profile named '{arg}'. try one of: "
                        f"{', '.join(available_names) or '(none)'}.",
                        synthetic=True,
                    )
                )
                return
            self._host._set_profile(target)
            return

        if text.startswith("/theme"):
            parts = text.split(maxsplit=1)
            available_names = [theme.name for theme in all_themes()]
            if len(parts) == 1:
                names = ", ".join(available_names) or "(none loaded)"
                hint = (
                    f"current theme: {self._host.theme.name} {self._host.theme.icon}. "
                    f"Available: {names}. Use /theme <name> or Ctrl+T to cycle."
                )
                self._append(self._message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._host._cycle_theme()
                return
            target = get_theme(arg)
            if target is None:
                self._append(
                    self._message(
                        "successor",
                        f"no theme named '{arg}'. try one of: "
                        f"{', '.join(available_names) or '(none)'}.",
                        synthetic=True,
                    )
                )
                return
            self._host._set_theme(target)
            return

        if text.startswith("/mode"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                hint = (
                    f"display mode: {self._host.display_mode}. "
                    f"Use /mode dark|light|toggle or Alt+D to flip. "
                    f"Mode is independent of theme — switching mode keeps "
                    f"the same theme."
                )
                self._append(self._message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "toggle":
                self._host._toggle_display_mode()
                return
            if arg in ("dark", "light"):
                self._host._set_display_mode(arg)
                return
            self._append(
                self._message(
                    "successor",
                    f"unknown /mode argument '{arg}'. try dark, light, or toggle.",
                    synthetic=True,
                )
            )
            return

        if text.startswith("/mouse"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                state = "on" if self._host._mouse_enabled else "off"
                hint = (
                    f"mouse: {state}. Off means the terminal owns wheel/selection. "
                    f"On enables in-chat wheel scrolling and clickable widgets; "
                    f"hold Shift to drag-select text."
                )
                self._append(self._message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "on":
                self._host._enable_mouse()
                self._append(
                    self._message(
                        "successor",
                        "mouse on. Click the title-bar widgets, use scroll wheel "
                        "to navigate history. Hold Shift while click-dragging to "
                        "use native text selection.",
                        synthetic=True,
                    )
                )
                return
            if arg == "off":
                self._host._disable_mouse()
                self._append(
                    self._message(
                        "successor",
                        "mouse off. The terminal owns wheel scrolling and native "
                        "click-drag selection again; clickable widgets are disabled.",
                        synthetic=True,
                    )
                )
                return
            if arg == "toggle":
                if self._host._mouse_enabled:
                    self._host._disable_mouse()
                else:
                    self._host._enable_mouse()
                return
            self._append(
                self._message(
                    "successor",
                    f"unknown /mouse argument '{arg}'. try on, off, or toggle.",
                    synthetic=True,
                )
            )
            return

        if text.startswith("/playback") or text.startswith("/review"):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            self._host._open_playback_from_chat(arg)
            return

        if text.startswith("/recording"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                state = "on" if bool(self._host._config.get("autorecord", True)) else "off"
                hint = (
                    f"recording: {state}. Auto-record writes local playback bundles under "
                    "~/.local/share/successor/recordings/ by default. Bundles stay on "
                    "local disk and pair playback.html with session_trace.json for debugging."
                )
                self._append(self._message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "on":
                self._host._set_autorecord(True)
                self._append(
                    self._message(
                        "successor",
                        "auto-record on. Future chat sessions will save local playback bundles "
                        "for debugging and harness-building.",
                        synthetic=True,
                    )
                )
                return
            if arg == "off":
                self._host._set_autorecord(False)
                self._append(
                    self._message(
                        "successor",
                        "auto-record off. Future chat sessions will stop writing playback bundles.",
                        synthetic=True,
                    )
                )
                return
            if arg == "toggle":
                enabled = not bool(self._host._config.get("autorecord", True))
                self._host._set_autorecord(enabled)
                state = "on" if enabled else "off"
                self._append(
                    self._message(
                        "successor",
                        f"auto-record {state}.",
                        synthetic=True,
                    )
                )
                return
            self._append(
                self._message(
                    "successor",
                    f"unknown /recording argument '{arg}'. try on, off, or toggle.",
                    synthetic=True,
                )
            )
            return

        if text.startswith("/density"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                names = ", ".join(d.name for d in self._densities)
                hint = (
                    f"current density: {self._host.density.name}. "
                    f"Available: {names}. Use /density <name> or Alt+=/Alt+- "
                    f"or Ctrl+] to cycle."
                )
                self._append(self._message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._host._cycle_density()
                return
            target = self._find_density(arg)
            if target is None:
                self._append(
                    self._message(
                        "successor",
                        f"no density named '{arg}'. try one of: "
                        f"{', '.join(d.name for d in self._densities)}.",
                        synthetic=True,
                    )
                )
                return
            self._host._set_density(target)
            return

        self._append(self._message("user", text))
        self._host._scroll_to_bottom()
        self._host._agent_turn = 0
        self._host._transient_retry_count = 0
        self._host._task_adoption_last_kind = ""
        self._host._task_continue_nudged_this_turn = False
        self._host._task_continue_nudge = None
        self._host._browser_verification_active = False
        self._host._browser_verification_reason = ""
        self._host._verification_continue_nudged_this_turn = False
        self._host._verification_continue_nudge = None
        self._host._verification_adoption_nudged_this_turn = False
        self._host._verification_adoption_nudge = None
        self._host._file_tool_continue_nudged_this_turn = False
        self._host._file_tool_continue_nudge = None
        self._host._subagent_continue_nudged_this_turn = False
        self._host._subagent_continue_nudge = None
        self._host._autocompact_attempted_this_turn = False
        self._host._begin_agent_turn()

    def build_turn_request_envelope_preview(
        self,
        *,
        turn_number: int | None = None,
    ) -> TurnRequestEnvelope:
        preview_turn = turn_number if turn_number is not None else max(1, self._host._agent_turn + 1)
        prepared = self._prepare_turn_request(
            turn_number=preview_turn,
            consume_one_shot_nudges=False,
        )
        return prepared.envelope

    def _stable_prompt_cache_break_reasons(
        self,
        previous_key: tuple[tuple[str, str], ...] | None,
        current_key: tuple[tuple[str, str], ...],
    ) -> tuple[str, ...]:
        if previous_key is None or previous_key == current_key:
            return ()
        previous = dict(previous_key)
        current = dict(current_key)
        changed: list[str] = []
        for key in sorted(set(previous) | set(current)):
            if previous.get(key) == current.get(key):
                continue
            changed.append(
                _STABLE_SECTION_REASON_LABELS.get(
                    key,
                    key.replace("_", " "),
                )
            )
        return tuple(changed)

    def _stable_system_prompt_bundle(
        self,
        *,
        enabled_tools: list[str],
        enabled_skills: list[Any],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[PromptSection, ...], str, bool]:
        sections: list[PromptSection] = [
            PromptSection(
                key="base",
                label="Base Prompt",
                content=self._host.system_prompt,
            )
        ]
        capabilities = self._host._detect_client_runtime_capabilities()
        effective_cwd = ""
        repo_verification_guidance = ""
        if enabled_tools and (
            "bash" in enabled_tools
            or any(name in {"read_file", "write_file", "edit_file"} for name in enabled_tools)
        ):
            effective_cwd = self._host._tool_working_directory()
            sections.append(
                PromptSection(
                    key="working_directory",
                    label="Working Directory",
                    content=(
                        "## Working directory\n\n"
                        f"Native file tools and bash both resolve relative paths "
                        f"from `cwd={effective_cwd}`. If the user asks for a file "
                        f"in a specific location (like `~/Desktop/foo.html`), use "
                        f"the absolute path — do not assume your cwd is what the "
                        f"user had in mind.\n\n"
                        f"## Working with tool results\n\n"
                        f"Before making each tool call, scan the conversation "
                        f"history above and check what you have ALREADY done. "
                        f"A tool result with no stdout means the command "
                        f"succeeded — that is normal for writes, redirects, "
                        f"`mkdir`, `touch`, `chmod`, and most mutating "
                        f"commands. NEVER re-issue a tool call that already "
                        f"appears earlier in the conversation; instead, take "
                        f"the next step toward the user's goal, or respond "
                        f"with plain text if you are done. Plain text "
                        f"(no tool call) is how you finish the task and "
                        f"return control to the user."
                    ),
                )
            )
            repo_verification_guidance = build_repo_verification_guidance(
                effective_cwd,
            )
            if repo_verification_guidance:
                sections.append(
                    PromptSection(
                        key="repo_verification",
                        label="Repository Verification",
                        content=repo_verification_guidance,
                    )
                )
        if enabled_tools:
            sections.append(
                PromptSection(
                    key="execution_discipline",
                    label="Execution Discipline",
                    content=(
                        "## Execution discipline\n\n"
                        "Use tools whenever they materially improve correctness, "
                        "completeness, grounding, or verification. Do not stop "
                        "early when another tool call would materially improve the "
                        "result. If you say you will inspect, edit, run, verify, or "
                        "check something, make the corresponding tool call in the "
                        "SAME response instead of promising future action. If a tool "
                        "returns partial, empty, or unhelpful results, change "
                        "strategy instead of blindly repeating the same call. Keep "
                        "working until the task is complete AND verified, then end "
                        "with plain text. Before reporting completion, verify that "
                        "the changed behavior actually works, and report failing "
                        "checks faithfully instead of implying success."
                    ),
                )
            )
            if "task" in enabled_tools:
                sections.append(
                    PromptSection(
                        key="task_primer",
                        label="Task Primer",
                        content=build_task_execution_primer(),
                    )
                )
            if "verify" in enabled_tools:
                sections.append(
                    PromptSection(
                        key="verification_primer",
                        label="Verification Primer",
                        content=build_verification_execution_primer(
                            subagent_available="subagent" in enabled_tools,
                            stateful_runtime=False,
                        ),
                    )
                )
            if "runbook" in enabled_tools:
                sections.append(
                    PromptSection(
                        key="runbook_primer",
                        label="Runbook Primer",
                        content=build_runbook_execution_primer(),
                    )
                )
            if bool(getattr(capabilities, "supports_parallel_tool_calls", False)):
                sections.append(
                    PromptSection(
                        key="parallel_tool_calls",
                        label="Parallel Tool Calls",
                        content=(
                            "## Parallel tool calls\n\n"
                            "When multiple tool calls are independent and the "
                            "result of one does not determine the arguments of "
                            "another, emit them in the SAME assistant turn instead "
                            "of serializing them one-by-one. This is especially "
                            "useful for parallel read-only inspection such as "
                            "multiple `read_file` calls, read-only `bash` checks, "
                            "or separate `holonet` lookups. Keep dependent steps, "
                            "writes, browser interaction sequences, and any "
                            "read-after-write verification serialized."
                        ),
                    )
                )
        tool_guidance = build_model_tool_guidance(enabled_tools)
        if tool_guidance:
            sections.append(
                PromptSection(
                    key="tool_guidance",
                    label="Tool Guidance",
                    content=tool_guidance,
                )
            )
        skill_hints = build_skill_hint_section(enabled_skills)
        if skill_hints:
            sections.append(
                PromptSection(
                    key="skill_hints",
                    label="Skill Hints",
                    content=skill_hints,
                )
            )
        skill_discovery = build_skill_discovery_section(
            enabled_skills,
            context_window_tokens=self._host._resolve_context_window(),
        )
        if skill_discovery:
            sections.append(
                PromptSection(
                    key="skill_discovery",
                    label="Skill Discovery",
                    content=skill_discovery,
                )
            )

        # Kimi compatibility nudge — when a profile is flagged as
        # kimi_compat, inject a reminder so the model uses Successor's
        # native tool names instead of Kimi CLI names.
        if getattr(self._host.profile, "kimi_compat", False):
            sections.append(
                PromptSection(
                    key="kimi_compat_nudge",
                    label="Kimi Compatibility",
                    content=(
                        "## Tool name reminder\n\n"
                        "You are running inside Successor. Use the exact tool names provided above:\n"
                        "- `read_file` (not `ReadFile`)\n"
                        "- `write_file` (not `WriteFile`)\n"
                        "- `edit_file` (not `StrReplaceFile`)\n"
                        "- `bash` (not `Shell`)\n"
                        "- `subagent` (not `Agent`)\n"
                        "- `holonet` for web search and fetch\n"
                        "Use `file_path` not `path`, `offset` not `line_offset`, "
                        "`limit` not `n_lines`."
                    ),
                )
            )

        key = tuple((section.key, section.content) for section in sections)
        cached_hit = (
            self._host._stable_prompt_cache_key == key
            and bool(self._host._stable_prompt_cache_sections)
            and self._host._stable_prompt_cache_prompt
        )
        if cached_hit:
            return (
                key,
                self._host._stable_prompt_cache_sections,
                self._host._stable_prompt_cache_prompt,
                True,
            )
        prompt = join_prompt_sections(sections)
        frozen_sections = tuple(sections)
        self._host._stable_prompt_cache_key = key
        self._host._stable_prompt_cache_sections = frozen_sections
        self._host._stable_prompt_cache_prompt = prompt
        return key, frozen_sections, prompt, False

    def _volatile_turn_sections(
        self,
        *,
        enabled_tools: list[str],
        enabled_skills: list[Any],
        latest_user_text: str,
        active_task_text: str,
        task_adoption_decision: Any,
        adoption_decision: Any,
        consume_one_shot_nudges: bool,
    ) -> list[PromptSection]:
        sections: list[PromptSection] = []
        stateful_runtime = adoption_decision.stateful_runtime

        if "task" in enabled_tools and self._host._task_ledger.items:
            sections.append(
                PromptSection(
                    key="task_runtime",
                    label="Task Runtime Context",
                    content=build_task_execution_guidance(self._host._task_ledger),
                    cache_break=True,
                    reason="task ledger state changes as work progresses",
                )
            )
            sections.append(
                PromptSection(
                    key="task_ledger",
                    label="Task Ledger",
                    content=build_task_prompt_section(self._host._task_ledger),
                    cache_break=True,
                    reason="task ledger state changes as work progresses",
                )
            )

        if "verify" in enabled_tools and (
            self._host._verification_ledger.items or stateful_runtime
        ):
            sections.append(
                PromptSection(
                    key="verification_runtime",
                    label="Verification Runtime Context",
                    content=build_verification_execution_guidance(
                        self._host._verification_ledger,
                        subagent_available="subagent" in enabled_tools,
                        stateful_runtime=stateful_runtime,
                    ),
                    cache_break=True,
                    reason="verification state and runtime evidence needs evolve per turn",
                )
            )
            if self._host._verification_ledger.items:
                sections.append(
                    PromptSection(
                        key="verification_ledger",
                        label="Verification Ledger",
                        content=build_verification_prompt_section(
                            self._host._verification_ledger
                        ),
                        cache_break=True,
                        reason="verification state and runtime evidence needs evolve per turn",
                    )
                )

        if "runbook" in enabled_tools and self._host._runbook.state is not None:
            sections.append(
                PromptSection(
                    key="runbook_runtime",
                    label="Runbook Runtime Context",
                    content=build_runbook_execution_guidance(self._host._runbook),
                    cache_break=True,
                    reason="runbook baseline and hypothesis state evolve per turn",
                )
            )
            sections.append(
                PromptSection(
                    key="runbook",
                    label="Runbook",
                    content=build_runbook_prompt_section(self._host._runbook),
                    cache_break=True,
                    reason="runbook baseline and hypothesis state evolve per turn",
                )
            )

        if self._host._browser_verification_active and "browser" in enabled_tools:
            browser_verification_guidance = build_browser_verification_guidance(
                latest_user_text=latest_user_text,
                active_task_text=active_task_text,
                vision_available="vision" in enabled_tools,
                browser_verifier_available=(
                    "skill" in enabled_tools
                    and any(skill.name == "browser-verifier" for skill in enabled_skills)
                ),
                browser_verifier_loaded=self._host._skill_already_loaded("browser-verifier"),
            )
            if browser_verification_guidance:
                sections.append(
                    PromptSection(
                        key="browser_runtime",
                        label="Browser Runtime Context",
                        content=browser_verification_guidance,
                        cache_break=True,
                        reason="browser-verification mode follows the current task state",
                    )
                )

        if task_adoption_decision.should_nudge and any(
            name in enabled_tools for name in ("task", "runbook", "verify")
        ):
            sections.append(
                PromptSection(
                    key="planning_reminder",
                    label="Planning Reminder",
                    content=(
                        "## Planning Reminder\n\n"
                        f"{task_adoption_decision.text}"
                    ),
                    cache_break=True,
                    reason="planning nudges are turn-local adoption hints",
                )
            )

        task_continue_nudge = self._host._task_continue_nudge
        if task_continue_nudge:
            sections.append(
                PromptSection(
                    key="continuation_reminder",
                    label="Continuation Reminder",
                    content=(
                        "## Continuation Reminder\n\n"
                        f"{task_continue_nudge}"
                    ),
                    cache_break=True,
                    reason="continuation nudges are one-shot turn reminders",
                )
            )
            if consume_one_shot_nudges:
                self._host._task_continue_nudge = None
        verification_continue_nudge = self._host._verification_continue_nudge
        if verification_continue_nudge:
            sections.append(
                PromptSection(
                    key="browser_verification_reminder",
                    label="Browser Verification Reminder",
                    content=(
                        "## Browser Verification Reminder\n\n"
                        f"{verification_continue_nudge}"
                    ),
                    cache_break=True,
                    reason="verification nudges are one-shot turn reminders",
                )
            )
            if consume_one_shot_nudges:
                self._host._verification_continue_nudge = None
        verification_adoption_nudge = self._host._verification_adoption_nudge
        if verification_adoption_nudge:
            sections.append(
                PromptSection(
                    key="verification_setup_reminder",
                    label="Verification Setup Reminder",
                    content=(
                        "## Verification Setup Reminder\n\n"
                        f"{verification_adoption_nudge}"
                    ),
                    cache_break=True,
                    reason="verification adoption nudges are one-shot turn reminders",
                )
            )
            if consume_one_shot_nudges:
                self._host._verification_adoption_nudge = None
        file_tool_continue_nudge = self._host._file_tool_continue_nudge
        if file_tool_continue_nudge:
            sections.append(
                PromptSection(
                    key="file_tool_recovery_reminder",
                    label="File Tool Recovery Reminder",
                    content=(
                        "## File Tool Recovery Reminder\n\n"
                        f"{file_tool_continue_nudge}"
                    ),
                    cache_break=True,
                    reason="file-tool recovery nudges are one-shot turn reminders",
                )
            )
            if consume_one_shot_nudges:
                self._host._file_tool_continue_nudge = None
        subagent_continue_nudge = self._host._subagent_continue_nudge
        if subagent_continue_nudge:
            sections.append(
                PromptSection(
                    key="background_task_reminder",
                    label="Background Task Reminder",
                    content=(
                        "## Background Task Reminder\n\n"
                        f"{subagent_continue_nudge}"
                    ),
                    cache_break=True,
                    reason="subagent reminders are one-shot turn reminders",
                )
            )
            if consume_one_shot_nudges:
                self._host._subagent_continue_nudge = None
        return sections

    def _prepare_turn_request(
        self,
        *,
        turn_number: int,
        consume_one_shot_nudges: bool,
    ) -> _PreparedTurnRequest:
        enabled_tools = self._host._enabled_tools_for_turn()
        enabled_skills = self._host._enabled_skills_for_turn(enabled_tools)
        latest_user_text = self._host._latest_real_user_text()
        active_task_text = self._host._browser_verification_context_text()
        task_adoption_decision = maybe_build_task_adoption_nudge(
            latest_user_text=latest_user_text,
            active_task_text=active_task_text,
            ledger=self._host._task_ledger,
            runbook=self._host._runbook,
            messages=self._host.messages,
        )
        adoption_decision = maybe_build_verification_adoption_nudge(
            latest_user_text=latest_user_text,
            active_task_text=active_task_text,
            ledger=self._host._verification_ledger,
            messages=self._host.messages,
        )
        stable_prompt_key, stable_sections, system_prompt, stable_prompt_cache_hit = (
            self._stable_system_prompt_bundle(
                enabled_tools=enabled_tools,
                enabled_skills=enabled_skills,
            )
        )
        volatile_sections = self._volatile_turn_sections(
            enabled_tools=enabled_tools,
            enabled_skills=enabled_skills,
            latest_user_text=latest_user_text,
            active_task_text=active_task_text,
            task_adoption_decision=task_adoption_decision,
            adoption_decision=adoption_decision,
            consume_one_shot_nudges=consume_one_shot_nudges,
        )

        api_messages_list = self._host._build_api_messages_native(system_prompt)
        request_messages_list = api_messages_list
        volatile_context_text = _format_runtime_tail_context(volatile_sections)
        volatile_tail_applied = False
        if volatile_context_text:
            request_messages_list, volatile_tail_applied = _append_runtime_tail_context(
                request_messages_list,
                volatile_context_text,
            )
        continuation_prefill_applied = False
        if turn_number > 1 and not volatile_tail_applied:
            request_messages_list, continuation_prefill_applied = (
                _append_internal_continuation_prefill(api_messages_list)
            )
        tool_schemas = tuple(build_native_tool_schemas(enabled_tools))
        api_messages_hash = _hash_message_sequence(
            api_messages_list,
            tool_schemas=tool_schemas,
        )
        request_messages_hash = _hash_message_sequence(
            request_messages_list,
            tool_schemas=tool_schemas,
        )
        request_tail_kind = "none"
        prefix_messages = request_messages_list
        if volatile_tail_applied:
            request_tail_kind = "runtime_tail"
            prefix_messages = request_messages_list[:-1]
        elif continuation_prefill_applied:
            request_tail_kind = "continuation_prefill"
            prefix_messages = request_messages_list[:-1]
        request_prefix_hash = _hash_message_sequence(
            prefix_messages,
            tool_schemas=tool_schemas,
        )
        stable_system_hash = _hash_fragments(
            system_prompt,
            _canonical_json(tool_schemas),
        )
        volatile_tail_hash = _hash_fragments(volatile_context_text) if volatile_context_text else ""
        cache_break_reasons = ()
        if consume_one_shot_nudges:
            cache_break_reasons = self._stable_prompt_cache_break_reasons(
                self._host._last_sent_stable_prompt_key,
                stable_prompt_key,
            )
        request_slot_id = getattr(self._host.client, "preferred_slot_id", None)
        if not isinstance(request_slot_id, int):
            request_slot_id = None
        request_cache_prompt = getattr(self._host.client, "use_prompt_cache", None)
        if not isinstance(request_cache_prompt, bool):
            request_cache_prompt = None
        envelope = TurnRequestEnvelope(
            turn=turn_number,
            system_sections=stable_sections,
            system_prompt=system_prompt,
            api_messages=tuple(api_messages_list),
            request_messages=tuple(request_messages_list),
            api_messages_hash=api_messages_hash,
            request_prefix_hash=request_prefix_hash,
            request_messages_hash=request_messages_hash,
            request_tail_kind=request_tail_kind,
            tool_schemas=tool_schemas,
            enabled_tools=tuple(enabled_tools),
            enabled_skills=tuple(skill.name for skill in enabled_skills),
            volatile_sections=tuple(volatile_sections),
            continuation_prefill_applied=continuation_prefill_applied,
            volatile_tail_applied=volatile_tail_applied,
            browser_verification_active=self._host._browser_verification_active,
            browser_verification_reason=self._host._browser_verification_reason,
            last_user_excerpt=_find_last_user_excerpt(api_messages_list),
            stable_system_hash=stable_system_hash,
            volatile_tail_hash=volatile_tail_hash,
            cache_break_reasons=cache_break_reasons,
            request_slot_id=request_slot_id,
            request_cache_prompt=request_cache_prompt,
        )
        return _PreparedTurnRequest(
            envelope=envelope,
            task_adoption_decision=task_adoption_decision,
            stable_prompt_key=stable_prompt_key,
            stable_prompt_cache_hit=stable_prompt_cache_hit,
        )

    def begin_agent_turn(self) -> None:
        """Open a new stream for the next turn of the agent loop."""
        if self._host._check_and_maybe_defer_for_autocompact():
            return

        self._host._agent_turn += 1
        max_agent_turns = max(
            1,
            int(
                getattr(
                    self._host.profile,
                    "max_agent_turns",
                    self._max_agent_turns_default,
                ) or self._max_agent_turns_default
            ),
        )
        if self._host._agent_turn > max_agent_turns:
            self._append(
                self._message(
                    "successor",
                    f"[agent loop halted at {max_agent_turns} turns — "
                    f"send a new message to continue]",
                    synthetic=True,
                )
            )
            self._host._agent_turn = 0
            return

        self._host._refresh_browser_verification_mode()
        prepared = self._prepare_turn_request(
            turn_number=self._host._agent_turn,
            consume_one_shot_nudges=True,
        )
        envelope = prepared.envelope

        if prepared.task_adoption_decision.should_nudge and any(
            name in envelope.enabled_tools
            for name in ("task", "runbook", "verify")
        ):
            if prepared.task_adoption_decision.kind != self._host._task_adoption_last_kind:
                self._host._trace_event(
                    "task_adoption_nudge",
                    turn=self._host._agent_turn,
                    kind=prepared.task_adoption_decision.kind,
                    long_horizon=prepared.task_adoption_decision.long_horizon,
                    stateful_runtime=prepared.task_adoption_decision.stateful_runtime,
                    recommend_runbook=prepared.task_adoption_decision.recommend_runbook,
                    browser_actions=prepared.task_adoption_decision.activity.browser_actions,
                    mutation_actions=prepared.task_adoption_decision.activity.mutation_actions,
                    verify_updates=prepared.task_adoption_decision.activity.verify_updates,
                )
                self._host._task_adoption_last_kind = prepared.task_adoption_decision.kind

        self._host._trace_event(
            "agent_turn_begin",
            turn=envelope.turn,
            continuation=(envelope.turn > 1),
            enabled_tools=list(envelope.enabled_tools),
            enabled_skills=list(envelope.enabled_skills),
            browser_verification_active=envelope.browser_verification_active,
            browser_verification_reason=envelope.browser_verification_reason,
            api_message_count=len(envelope.api_messages),
            request_message_count=len(envelope.request_messages),
            last_user_excerpt=envelope.last_user_excerpt,
            stable_system_hash=envelope.stable_system_hash,
            api_messages_hash=envelope.api_messages_hash,
            request_prefix_hash=envelope.request_prefix_hash,
            request_messages_hash=envelope.request_messages_hash,
            request_tail_kind=envelope.request_tail_kind,
            volatile_tail_hash=envelope.volatile_tail_hash,
            volatile_tail_applied=envelope.volatile_tail_applied,
            stable_prompt_cache_hit=prepared.stable_prompt_cache_hit,
            cache_break_reasons=list(envelope.cache_break_reasons),
            request_slot_id=envelope.request_slot_id,
            request_cache_prompt=envelope.request_cache_prompt,
        )
        if envelope.continuation_prefill_applied:
            self._host._trace_event(
                "assistant_prefill_guard_applied",
                turn=envelope.turn,
                prior_last_role="assistant",
            )
        try:
            if envelope.tool_schemas:
                self._host._stream = self._host.client.stream_chat(
                    messages=list(envelope.request_messages),
                    tools=list(envelope.tool_schemas),
                )
            else:
                self._host._stream = self._host.client.stream_chat(
                    messages=list(envelope.request_messages)
                )
        except Exception as exc:
            self._host._trace_event(
                "stream_open_failed",
                turn=self._host._agent_turn,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._host._last_sent_stable_prompt_key = prepared.stable_prompt_key
        self._host._remember_active_request_usage(envelope)
        self._host._mark_stream_opened()
        self._host._trace_event(
            "stream_opened",
            turn=envelope.turn,
            tool_schema_names=list(envelope.enabled_tools) if envelope.tool_schemas else [],
            stable_system_hash=envelope.stable_system_hash,
            request_prefix_hash=envelope.request_prefix_hash,
            request_messages_hash=envelope.request_messages_hash,
            request_tail_kind=envelope.request_tail_kind,
            request_slot_id=envelope.request_slot_id,
            request_cache_prompt=envelope.request_cache_prompt,
        )
        self._host._stream_content = []
        self._host._stream_reasoning_chars = 0
        if "bash" in envelope.enabled_tools:
            self._host._stream_bash_detector = BashStreamDetector()
        else:
            self._host._stream_bash_detector = None

    def build_api_messages_native(self, sys_prompt: str) -> list[dict]:
        """Build the api_messages list in native tool-call shape."""
        api_messages: list[dict] = [{"role": "system", "content": sys_prompt}]
        ordered = self._host._api_ordered_messages()

        i = 0
        n = len(ordered)
        while i < n:
            m = ordered[i]

            if m.is_summary:
                _append_api_message(
                    api_messages,
                    role="user",
                    content=(
                        "[summary of earlier conversation, provided by the "
                        "harness — treat as authoritative context, not a "
                        "user turn]\n\n" + m.raw_text
                    ),
                )
                i += 1
                continue

            if _api_role_for_message(m) == "user" and not _message_has_tool_artifact(m):
                if m.synthetic:
                    i += 1
                    continue
                _append_api_message(api_messages, role="user", content=m.raw_text)
                i += 1
                continue

            if _api_role_for_message(m) == "assistant" and not _message_has_tool_artifact(m):
                if m.synthetic:
                    i += 1
                    continue
                tool_cards: list[ToolArtifact] = []
                j = i + 1
                while j < n and _message_has_tool_artifact(ordered[j]):
                    card = _message_tool_artifact(ordered[j])
                    if card is not None:
                        tool_cards.append(card)
                    j += 1

                if tool_cards:
                    api_messages.append(_assistant_with_tool_calls(m.raw_text or "", tool_cards))
                    for card in tool_cards:
                        api_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": card.tool_call_id,
                                "content": _tool_card_content_for_api(card),
                            }
                        )
                else:
                    _append_api_message(api_messages, role="assistant", content=m.raw_text)
                i = j
                continue

            if _message_has_tool_artifact(m):
                card = _message_tool_artifact(m)
                if card is None:
                    i += 1
                    continue
                tool_cards = [card]
                j = i + 1
                while j < n and _message_has_tool_artifact(ordered[j]):
                    next_card = _message_tool_artifact(ordered[j])
                    if next_card is not None:
                        tool_cards.append(next_card)
                    j += 1
                api_messages.append(_assistant_with_tool_calls("", tool_cards))
                for card in tool_cards:
                    api_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": card.tool_call_id,
                            "content": _tool_card_content_for_api(card),
                        }
                    )
                i = j
                continue

            i += 1

        return api_messages

    def pump_stream(self) -> None:
        """Drain any pending stream events and update accumulators."""
        if self._host._stream is None:
            return

        events = self._host._stream.drain()
        for ev in events:
            if isinstance(ev, StreamStarted):
                self._host._trace_event(
                    "stream_started",
                    turn=self._host._agent_turn,
                    first_token_ms=self._host._mark_stream_started(),
                )
            elif isinstance(ev, ReasoningChunk):
                self._host._stream_reasoning_chars += len(ev.text)
            elif isinstance(ev, ContentChunk):
                self._host._stream_content.append(ev.text)
                if self._host._stream_bash_detector is not None:
                    self._host._stream_bash_detector.feed(ev.text)
            elif isinstance(ev, StreamEnded):
                raw_content = "".join(self._host._stream_content).strip()
                if self._host._stream_bash_detector is not None:
                    self._host._stream_bash_detector.flush()
                    display_content = self._host._stream_bash_detector.cleaned_text().strip()
                    legacy_blocks = self._host._stream_bash_detector.completed()
                    self._host._stream_bash_detector = None
                else:
                    display_content = raw_content
                    legacy_blocks = []
                native_calls = list(getattr(ev, "tool_calls", ()) or ())
                perf_snapshot = self._host._record_stream_perf(
                    turn=self._host._agent_turn,
                    finish_reason=ev.finish_reason,
                    raw_usage=ev.usage,
                    raw_timings=getattr(ev, "timings", None),
                )
                self._host._trace_event(
                    "stream_end",
                    turn=self._host._agent_turn,
                    finish_reason=ev.finish_reason,
                    finish_reason_reported=bool(getattr(ev, "finish_reason_reported", True)),
                    assistant_excerpt=_trace_clip_text(raw_content, limit=400),
                    reasoning_chars=self._host._stream_reasoning_chars,
                    native_tool_calls=[
                        _trace_tool_call_summary(tc) for tc in native_calls
                    ],
                    legacy_block_count=len(legacy_blocks),
                    first_token_ms=perf_snapshot.first_token_ms,
                    total_stream_ms=perf_snapshot.total_stream_ms,
                    provider_timings=(
                        perf_snapshot.timings.raw_timings
                        if perf_snapshot.timings is not None else None
                    ),
                    prompt_cache_hit_ratio=perf_snapshot.prompt_cache_hit_ratio,
                    cache_hit_tokens=perf_snapshot.cache_hit_tokens,
                    prompt_eval_tokens=perf_snapshot.prompt_eval_tokens,
                    prompt_eval_ms=perf_snapshot.prompt_eval_ms,
                    output_tokens=perf_snapshot.output_tokens,
                    generation_ms=perf_snapshot.generation_ms,
                    suspected_kv_miss=perf_snapshot.suspected_kv_miss,
                    suspected_kv_miss_reason=perf_snapshot.suspected_kv_miss_reason,
                )

                if raw_content:
                    self._append(
                        self._message(
                            "successor",
                            raw_content,
                            display_text=display_content,
                        )
                    )
                elif legacy_blocks or native_calls:
                    self._append(
                        self._message(
                            "successor",
                            "",
                            display_text="",
                        )
                    )
                else:
                    self._append(
                        self._message(
                            "successor",
                            "(no answer — model produced only reasoning)",
                            synthetic=True,
                        )
                    )
                self._host._last_usage = ev.usage
                self._host._finalize_active_request_usage(ev.usage)
                self._host._stream = None
                self._host._stream_content = []
                self._host._stream_reasoning_chars = 0
                self._host._streaming_preview_cache = {}

                any_ran = False
                if native_calls:
                    any_ran |= self._host._dispatch_native_tool_calls(
                        native_calls,
                        stream_finish_reason=ev.finish_reason,
                        stream_finish_reason_reported=bool(
                            getattr(ev, "finish_reason_reported", True)
                        ),
                    )
                if legacy_blocks:
                    any_ran |= self._host._dispatch_streamed_bash_blocks(legacy_blocks)
                self._host._trace_event(
                    "tool_dispatch_batch",
                    turn=self._host._agent_turn,
                    native_tool_count=len(native_calls),
                    legacy_block_count=len(legacy_blocks),
                    any_ran=any_ran,
                    runner_count=len(self._host._running_tools),
                )

                if any_ran and self._host._agent_turn > 0:
                    self._host._maybe_queue_verification_adoption_nudge()
                    if self._host._running_tools:
                        self._host._trace_event(
                            "continuation_pending",
                            turn=self._host._agent_turn,
                            runner_count=len(self._host._running_tools),
                        )
                        self._host._pending_continuation = True
                    else:
                        self._host._pending_continuation = False
                        self._host._trace_event(
                            "continuation_immediate",
                            turn=self._host._agent_turn,
                        )
                        self._host._begin_agent_turn()
                    return

                if (
                    self._host._agent_turn > 0
                    and self._host._task_ledger.has_in_progress()
                    and not self._host._task_continue_nudged_this_turn
                ):
                    nudge = build_task_continue_nudge(self._host._task_ledger)
                    if nudge:
                        self._host._task_continue_nudged_this_turn = True
                        self._host._task_continue_nudge = nudge
                        active = self._host._task_ledger.in_progress_task()
                        self._host._trace_event(
                            "task_continue_nudge",
                            turn=self._host._agent_turn,
                            active_task=active.active_form if active else "",
                            assistant_excerpt=_trace_clip_text(raw_content, limit=320),
                        )
                        self._host._begin_agent_turn()
                        return

                if (
                    self._host._agent_turn > 0
                    and self._host._verification_ledger.has_in_progress()
                    and not self._host._verification_continue_nudged_this_turn
                ):
                    nudge = build_verification_continue_nudge(
                        self._host._verification_ledger
                    )
                    if nudge:
                        self._host._verification_continue_nudged_this_turn = True
                        self._host._verification_continue_nudge = nudge
                        active = self._host._verification_ledger.in_progress_item()
                        self._host._trace_event(
                            "verification_continue_nudge",
                            turn=self._host._agent_turn,
                            active_claim=active.claim if active else "",
                            active_evidence=active.evidence if active else "",
                            assistant_excerpt=_trace_clip_text(raw_content, limit=320),
                        )
                        self._host._begin_agent_turn()
                        return

                self._host._trace_event(
                    "agent_turn_complete",
                    turn=self._host._agent_turn,
                    reason="plain_text_or_no_runnable_tools",
                )
                self._host._agent_turn = 0
            elif isinstance(ev, StreamError):
                partial = "".join(self._host._stream_content)
                self._host._trace_event(
                    "stream_error",
                    turn=self._host._agent_turn,
                    message=ev.message,
                    partial_excerpt=_trace_clip_text(partial, limit=400),
                )

                # ── OAuth recovery ──
                # When the profile uses OAuth and we get a 401/403, the
                # access token may have expired (or been revoked). Try an
                # immediate refresh. If it works, auto-retry the stream
                # so the user never sees the error. If it fails, show an
                # actionable message.
                lower_msg = (ev.message or "").lower()
                is_auth_err = "http 401" in lower_msg or "unauthorized" in lower_msg or "http 403" in lower_msg or "forbidden" in lower_msg
                if (
                    not partial
                    and is_auth_err
                    and self._host.profile.oauth is not None
                ):
                    refreshed = self._try_oauth_recovery()
                    if refreshed:
                        # Token refreshed — retry the stream immediately
                        self._host._stream = None
                        self.begin_agent_turn()
                        return
                    # Refresh failed — fall through to formatted error below,
                    # which will include the OAuth-specific message.

                # Pre-stream transient retry: if no content has been
                # delivered yet, the user hasn't seen anything. Safe to
                # back off and retry — no partial response to conflict
                # with.  We time.sleep here because the frame loop has
                # nothing to render (no content = blank stream area).
                if (
                    not partial
                    and is_transient_stream_error(ev.message or "")
                    and self._host._transient_retry_count < MAX_TRANSIENT_RETRIES
                ):
                    self._host._transient_retry_count += 1
                    delay = TRANSIENT_BACKOFF_BASE_S * (
                        2 ** (self._host._transient_retry_count - 1)
                    )
                    self._host._trace_event(
                        "transient_retry",
                        turn=self._host._agent_turn,
                        attempt=self._host._transient_retry_count,
                        max_attempts=MAX_TRANSIENT_RETRIES,
                        delay_s=delay,
                        reason=ev.message,
                    )
                    self._host._stream = None
                    time.sleep(delay)
                    # Re-kick the agent turn — begin_agent_turn opens a
                    # new stream and resets stream content buffers.
                    self.begin_agent_turn()
                    return

                if partial:
                    msg = f"{partial}\n\n[stream interrupted: {ev.message}]"
                else:
                    msg = self.format_stream_error(ev.message)
                self._append(self._message("successor", msg, synthetic=True))
                self._host._finalize_active_request_usage(None)
                self._host._clear_stream_perf_markers()
                self._host._stream = None
                self._host._stream_content = []
                self._host._stream_reasoning_chars = 0
                self._host._streaming_preview_cache = {}
                self._host._stream_bash_detector = None
                self._host._agent_turn = 0

    def _try_oauth_recovery(self) -> bool:
        """Attempt an immediate OAuth token refresh.

        Called when a 401/403 occurs on an OAuth-enabled profile. Returns
        True if the token was refreshed (caller should retry the stream),
        False if refresh failed or no refresh token is available.
        """
        import successor.oauth as _oauth_mod
        from .oauth.storage import load_token, save_token

        oauth_ref = self._host.profile.oauth
        token = load_token(oauth_ref.key)
        if token is None or not token.refresh_token:
            return False
        try:
            new_token = _oauth_mod.refresh_access_token(
                token.refresh_token,
                client_id=_oauth_mod.KIMI_CODE_CLIENT_ID,
                oauth_host=_oauth_mod.DEFAULT_OAUTH_HOST,
            )
        except Exception:
            return False
        save_token(oauth_ref.key, new_token)
        self._host.client.api_key = new_token.access_token
        self._host._trace_event(
            "oauth_recovery",
            turn=self._host._agent_turn,
            action="refresh_succeeded",
        )
        return True

    def format_stream_error(self, raw: str) -> str:
        """Translate a raw stream error into a friendlier hint."""
        provider_cfg = self._host.profile.provider or {}
        base_url = provider_cfg.get("base_url", "http://localhost:8080")
        lower = raw.lower()
        is_conn_refused = (
            "connection refused" in lower
            or "errno 111" in lower
            or "could not connect" in lower
        )
        is_dns = (
            "name or service not known" in lower
            or "nodename nor servname" in lower
            or "temporary failure in name resolution" in lower
        )
        is_unreachable = "network is unreachable" in lower
        is_timeout = (
            "timed out" in lower
            or "timeout" in lower
            or "the read operation timed out" in lower
        )
        if is_conn_refused or is_dns or is_unreachable or is_timeout:
            return (
                f"[no server at {base_url}]\n"
                f"\n"
                f"successor expects an OpenAI-compatible HTTP endpoint at\n"
                f"the URL above. Three ways to fix this:\n"
                f"\n"
                f"  1. Start a local llama.cpp server:\n"
                f"     llama-server -m <your-model.gguf> --host 0.0.0.0 --port 8080\n"
                f"\n"
                f"  2. Quit (Ctrl+C) and run `successor setup` to create\n"
                f"     a profile against OpenAI or OpenRouter instead.\n"
                f"\n"
                f"  3. Open /config and edit the active profile's\n"
                f"     provider.base_url and provider.api_key fields."
            )
        is_auth = "http 401" in lower or "unauthorized" in lower
        is_forbidden = "http 403" in lower or "forbidden" in lower
        if is_auth or is_forbidden:
            has_oauth = self._host.profile.oauth is not None
            if has_oauth:
                return (
                    f"[auth failed — {base_url}]\n"
                    f"\n"
                    f"OAuth token refresh failed. The stored credentials may\n"
                    f"have expired or been revoked.\n"
                    f"\n"
                    f"Run `successor login` to re-authenticate, then restart\n"
                    f"the chat."
                )
            return (
                f"[unauthorized — {base_url}]\n"
                f"\n"
                f"The server rejected the request as unauthorized. Either\n"
                f"the api_key is missing, malformed, or revoked. Open\n"
                f"/config and check the active profile's provider.api_key\n"
                f"field."
            )
        if "http 402" in lower or "payment required" in lower:
            return (
                f"[out of credits — {base_url}]\n"
                f"\n"
                f"The provider says the account is out of credits or owes\n"
                f"a balance. Top up at the provider dashboard, then retry."
            )
        if "http 429" in lower or "too many requests" in lower:
            return (
                f"[rate limited by {base_url}]\n"
                f"\n"
                f"The provider is throttling requests. Wait a moment and\n"
                f"retry, or switch to a different model / paid tier in\n"
                f"the active profile via /config."
            )
        return f"[stream failed: {raw}]"
