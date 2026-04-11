"""Semantic in-flight preview model for streamed native tool calls.

The provider only gives us partial tool-call snapshots while a stream is
in flight: an optional function name plus a growing raw JSON arguments
blob. The renderer should not have to rediscover tool semantics from
scratch every frame. This module turns those partial snapshots into a
typed preview state with stable glyphs, labels, and best-effort hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from .bash.cards import ToolCard
from .file_tools import (
    edit_file_preview_card,
    read_file_preview_card,
    write_file_preview_card,
)
from .web.browser import browser_preview_card
from .web.holonet import HolonetRoute, holonet_preview_card
from .web.vision import vision_preview_card

PreviewState = Literal["pending_name", "known", "unsupported"]

_NUMBER_FIELD_TEMPLATE = r'"{field}"\s*:\s*(-?\d+)'
_BOOL_FIELD_TEMPLATE = r'"{field}"\s*:\s*(true|false)'
_PREFERRED_HINT_KEYS: dict[str, tuple[str, ...]] = {
    "read_file": ("path", "offset", "limit"),
    "write_file": ("path",),
    "edit_file": ("path", "replace_all"),
    "browser": ("target", "url", "text", "option", "key", "scope", "action"),
    "holonet": ("query", "provider", "count"),
    "vision": ("path", "detail", "prompt"),
    "skill": ("skill", "task"),
    "task": ("active", "status"),
    "verify": ("active", "status"),
    "runbook": ("objective", "decision", "status"),
}


@dataclass(frozen=True, slots=True)
class StreamingToolPreview:
    """Semantic identity for an in-flight tool call."""

    state: PreviewState
    tool_name: str
    glyph: str
    label: str
    display_text: str = ""
    hint: str = ""
    status: str = ""
    sticky: bool = False

    @property
    def header_key(self) -> tuple[str, str, str, str]:
        return (self.tool_name, self.glyph, self.label, self.hint)


def build_streaming_tool_preview(
    *,
    name: str,
    raw_arguments: str,
    prior: StreamingToolPreview | None = None,
) -> StreamingToolPreview:
    """Build a semantic preview for one in-flight streamed tool call."""
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return StreamingToolPreview(
            state="pending_name",
            tool_name="",
            glyph="◌",
            label="pending tool",
            display_text=raw_arguments,
            status="resolving tool name…",
        )

    if normalized_name == "bash":
        return _build_bash_preview(raw_arguments, prior=prior)

    builder = _STREAMING_BUILDERS.get(normalized_name)
    if builder is None:
        return StreamingToolPreview(
            state="unsupported",
            tool_name=normalized_name,
            glyph="?",
            label=normalized_name,
            display_text=raw_arguments,
            status="no preview adapter",
        )

    preview = builder(raw_arguments)
    if preview.sticky:
        return preview
    if (
        prior is not None
        and prior.tool_name == normalized_name
        and prior.sticky
        and preview.state == "known"
    ):
        return replace(prior, display_text=preview.display_text or prior.display_text)
    return preview


def _build_bash_preview(
    raw_arguments: str,
    *,
    prior: StreamingToolPreview | None = None,
) -> StreamingToolPreview:
    command_text = _extract_command_tail(raw_arguments)
    command_text = command_text or raw_arguments
    if command_text and len(command_text.strip()) >= 3:
        try:
            from .bash import preview_bash
            from .bash.verbclass import glyph_for_class, verb_class_for

            card = preview_bash(command_text)
        except Exception:
            card = None
        if card is not None and card.confidence >= 0.7:
            cls = verb_class_for(card.verb, card.risk)
            glyph = glyph_for_class(cls)
            hint = _first_hint_from_card(card)
            return StreamingToolPreview(
                state="known",
                tool_name="bash",
                glyph=glyph,
                label=card.verb,
                display_text=command_text,
                hint=hint,
                sticky=True,
            )

    if prior is not None and prior.tool_name == "bash" and prior.sticky:
        return replace(prior, display_text=command_text or prior.display_text)

    return StreamingToolPreview(
        state="known",
        tool_name="bash",
        glyph="$",
        label="bash",
        display_text=command_text,
        status="receiving command…",
    )


def _build_read_file_preview(raw_arguments: str) -> StreamingToolPreview:
    args: dict[str, object] = {}
    path = _extract_json_string_field(raw_arguments, "file_path")
    offset = _extract_json_int_field(raw_arguments, "offset")
    limit = _extract_json_int_field(raw_arguments, "limit")
    if path:
        args["file_path"] = path
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    card = read_file_preview_card(args, tool_call_id="")
    return _card_preview(
        card,
        display_text=path or raw_arguments,
        status=("receiving path…" if not path else "receiving arguments…"),
        sticky=bool(path),
    )


def _build_write_file_preview(raw_arguments: str) -> StreamingToolPreview:
    args: dict[str, object] = {}
    path = _extract_json_string_field(raw_arguments, "file_path")
    if path:
        args["file_path"] = path
    card = write_file_preview_card(args, tool_call_id="")
    content = _extract_json_string_field(raw_arguments, "content")
    status = "receiving path…" if not path else ("receiving content…" if not content else "")
    display_text = path or raw_arguments
    return _card_preview(card, display_text=display_text, status=status, sticky=bool(path))


def _build_edit_file_preview(raw_arguments: str) -> StreamingToolPreview:
    args: dict[str, object] = {}
    path = _extract_json_string_field(raw_arguments, "file_path")
    replace_all = _extract_json_bool_field(raw_arguments, "replace_all")
    if path:
        args["file_path"] = path
    if replace_all is not None:
        args["replace_all"] = replace_all
    card = edit_file_preview_card(args, tool_call_id="")
    old_string = _extract_json_string_field(raw_arguments, "old_string")
    status = "receiving path…" if not path else ("receiving edit fragment…" if not old_string else "")
    display_text = path or raw_arguments
    return _card_preview(card, display_text=display_text, status=status, sticky=bool(path))


def _build_browser_preview(raw_arguments: str) -> StreamingToolPreview:
    action = _extract_json_string_field(raw_arguments, "action")
    if not action:
        return StreamingToolPreview(
            state="known",
            tool_name="browser",
            glyph="◉",
            label="browser",
            display_text=raw_arguments,
            status="receiving action…",
        )
    args: dict[str, object] = {"action": action}
    for field in ("target", "url", "text", "option", "key", "scope"):
        value = _extract_json_string_field(raw_arguments, field)
        if value:
            args[field] = value
    card = browser_preview_card(args, tool_call_id="")
    display_text = " ".join(
        bit
        for bit in (
            str(args.get("action") or ""),
            str(args.get("target") or args.get("url") or args.get("text") or ""),
            str(args.get("option") or ""),
            str(args.get("key") or ""),
        )
        if bit
    ) or raw_arguments
    return _card_preview(
        card,
        display_text=display_text,
        status=("receiving arguments…" if len(args) <= 1 else ""),
        sticky=True,
    )


def _build_holonet_preview(raw_arguments: str) -> StreamingToolPreview:
    provider = _extract_json_string_field(raw_arguments, "provider") or "auto"
    query = _extract_json_string_field(raw_arguments, "query")
    url = _extract_json_string_field(raw_arguments, "url")
    count = _extract_json_int_field(raw_arguments, "count") or 5
    if not query and not url and provider == "auto":
        return StreamingToolPreview(
            state="known",
            tool_name="holonet",
            glyph="≈",
            label="holonet",
            display_text=raw_arguments,
            status="receiving query…",
        )
    route = HolonetRoute(provider=provider, query=query, url=url, count=count)
    card = holonet_preview_card(route, tool_call_id="")
    display_text = " ".join(bit for bit in (provider, query or url) if bit) or raw_arguments
    return _card_preview(
        card,
        display_text=display_text,
        status=("receiving arguments…" if not (query or url) else ""),
        sticky=bool(query or url or provider != "auto"),
    )


def _build_vision_preview(raw_arguments: str) -> StreamingToolPreview:
    args: dict[str, object] = {}
    for field in ("path", "prompt", "detail"):
        value = _extract_json_string_field(raw_arguments, field)
        if value:
            args[field] = value
    card = vision_preview_card(args, tool_call_id="")
    path = str(args.get("path") or "")
    prompt = str(args.get("prompt") or "")
    display_text = " ".join(bit for bit in (path, prompt) if bit) or raw_arguments
    status = "receiving image path…" if not path else ("receiving prompt…" if not prompt else "")
    return _card_preview(card, display_text=display_text, status=status, sticky=bool(path))


def _build_skill_preview(raw_arguments: str) -> StreamingToolPreview:
    skill = _extract_json_string_field(raw_arguments, "skill")
    task = _extract_json_string_field(raw_arguments, "task")
    params: list[tuple[str, str]] = []
    if skill:
        params.append(("skill", skill))
    if task:
        params.append(("task", _truncate_value(task, 64)))
    card = ToolCard(
        verb="load-skill",
        params=tuple(params),
        risk="safe",
        raw_command=" ".join(bit for bit in (skill, task) if bit) or "skill",
        confidence=1.0,
        parser_name="native-skill",
        tool_name="skill",
        tool_arguments={
            "skill": skill,
            **({"task": task} if task else {}),
        },
        raw_label_prefix="§",
        tool_call_id="",
    )
    return _card_preview(
        card,
        display_text=" ".join(bit for bit in (skill, task) if bit) or raw_arguments,
        status=("receiving skill name…" if not skill else ""),
        sticky=bool(skill),
    )


def _build_task_preview(raw_arguments: str) -> StreamingToolPreview:
    active = _extract_json_string_field(raw_arguments, "content")
    status_value = _extract_json_string_field(raw_arguments, "status")
    params: list[tuple[str, str]] = []
    if active:
        params.append(("active", _truncate_value(active, 64)))
    if status_value:
        params.append(("status", status_value))
    card = ToolCard(
        verb="task-ledger",
        params=tuple(params),
        risk="safe",
        raw_command="update tasks",
        confidence=1.0,
        parser_name="native-task",
        tool_name="task",
        raw_label_prefix="#",
        tool_call_id="",
    )
    return _card_preview(
        card,
        display_text=active or raw_arguments,
        status=("receiving items…" if not active else ""),
        sticky=bool(active),
    )


def _build_verify_preview(raw_arguments: str) -> StreamingToolPreview:
    claim = _extract_json_string_field(raw_arguments, "claim")
    status_value = _extract_json_string_field(raw_arguments, "status")
    params: list[tuple[str, str]] = []
    if claim:
        params.append(("active", _truncate_value(claim, 64)))
    if status_value:
        params.append(("status", status_value))
    card = ToolCard(
        verb="verification",
        params=tuple(params),
        risk="safe",
        raw_command="update verification contract",
        confidence=1.0,
        parser_name="native-verify",
        tool_name="verify",
        raw_label_prefix="✓",
        tool_call_id="",
    )
    return _card_preview(
        card,
        display_text=claim or raw_arguments,
        status=("receiving assertions…" if not claim else ""),
        sticky=bool(claim),
    )


def _build_runbook_preview(raw_arguments: str) -> StreamingToolPreview:
    objective = _extract_json_string_field(raw_arguments, "objective")
    status_value = _extract_json_string_field(raw_arguments, "status")
    decision = _extract_json_string_field(raw_arguments, "decision")
    params: list[tuple[str, str]] = []
    if status_value:
        params.append(("status", status_value))
    if objective:
        params.append(("objective", _truncate_value(objective, 64)))
    if decision:
        params.append(("decision", decision))
    card = ToolCard(
        verb="runbook",
        params=tuple(params),
        risk="safe",
        raw_command="update runbook",
        confidence=1.0,
        parser_name="native-runbook",
        tool_name="runbook",
        raw_label_prefix="◇",
        tool_call_id="",
    )
    return _card_preview(
        card,
        display_text=objective or raw_arguments,
        status=("receiving state…" if not (objective or status_value or decision) else ""),
        sticky=bool(objective or status_value or decision),
    )


def _build_subagent_preview(raw_arguments: str) -> StreamingToolPreview:
    prompt = _extract_json_string_field(raw_arguments, "prompt")
    role = _extract_json_string_field(raw_arguments, "role")
    name = _extract_json_string_field(raw_arguments, "name")
    bits = [bit for bit in (role, name, prompt) if bit]
    hint = ""
    if role:
        hint = f"role: {role}"
    elif name:
        hint = f"name: {name}"
    return StreamingToolPreview(
        state="known",
        tool_name="subagent",
        glyph="↗",
        label="subagent",
        display_text=" ".join(bits) or raw_arguments,
        hint=hint,
        status=("receiving prompt…" if not prompt else ""),
        sticky=bool(prompt or role or name),
    )


def _card_preview(
    card: ToolCard,
    *,
    display_text: str,
    status: str = "",
    sticky: bool = False,
) -> StreamingToolPreview:
    return StreamingToolPreview(
        state="known",
        tool_name=card.tool_name,
        glyph=(card.raw_label_prefix or "⟡").strip()[:1] or "⟡",
        label=card.verb,
        display_text=display_text,
        hint=_first_hint_from_card(card),
        status=status,
        sticky=sticky,
    )


def _first_hint_from_card(card: ToolCard) -> str:
    params = list(card.params)
    preferred_keys = _PREFERRED_HINT_KEYS.get(card.tool_name, ())
    if preferred_keys:
        param_map = {key: value for key, value in params}
        for key in preferred_keys:
            value = param_map.get(key)
            if not value or value == "(missing)":
                continue
            text = str(value).replace("\n", " ").strip()
            if len(text) > 50:
                text = text[:47] + "…"
            return f"{key}: {text}"
    for key, value in params:
        if not value or value == "(missing)":
            continue
        text = str(value).replace("\n", " ").strip()
        if len(text) > 50:
            text = text[:47] + "…"
        return f"{key}: {text}"
    return ""


def _extract_command_tail(raw_args: str) -> str:
    if not raw_args:
        return ""
    for key_marker in ('"command":"', '"command": "'):
        idx = raw_args.find(key_marker)
        if idx == -1:
            continue
        body = raw_args[idx + len(key_marker):]
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == '"':
                break
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt == "n":
                    out.append("\n")
                    i += 2
                    continue
                if nxt == "t":
                    out.append("\t")
                    i += 2
                    continue
                if nxt == "r":
                    out.append("\r")
                    i += 2
                    continue
                if nxt in ('"', "\\", "/"):
                    out.append(nxt)
                    i += 2
                    continue
                out.append("\\")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return raw_args


def _extract_json_string_field(raw_args: str, field: str) -> str:
    if not raw_args:
        return ""
    markers = (f'"{field}":"', f'"{field}": "')
    for key_marker in markers:
        idx = raw_args.find(key_marker)
        if idx == -1:
            continue
        body = raw_args[idx + len(key_marker):]
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == '"':
                break
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt == "n":
                    out.append("\n")
                    i += 2
                    continue
                if nxt == "t":
                    out.append("\t")
                    i += 2
                    continue
                if nxt == "r":
                    out.append("\r")
                    i += 2
                    continue
                if nxt in ('"', "\\", "/"):
                    out.append(nxt)
                    i += 2
                    continue
                out.append("\\")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return ""


def _extract_json_int_field(raw_args: str, field: str) -> int | None:
    if not raw_args:
        return None
    pattern = re.compile(_NUMBER_FIELD_TEMPLATE.format(field=re.escape(field)))
    match = pattern.search(raw_args)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_json_bool_field(raw_args: str, field: str) -> bool | None:
    if not raw_args:
        return None
    pattern = re.compile(_BOOL_FIELD_TEMPLATE.format(field=re.escape(field)))
    match = pattern.search(raw_args)
    if not match:
        return None
    return match.group(1) == "true"


def _truncate_value(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_STREAMING_BUILDERS = {
    "read_file": _build_read_file_preview,
    "write_file": _build_write_file_preview,
    "edit_file": _build_edit_file_preview,
    "browser": _build_browser_preview,
    "holonet": _build_holonet_preview,
    "vision": _build_vision_preview,
    "skill": _build_skill_preview,
    "task": _build_task_preview,
    "verify": _build_verify_preview,
    "runbook": _build_runbook_preview,
    "subagent": _build_subagent_preview,
}
