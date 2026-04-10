"""Deterministic live progress summaries for long-running sessions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bash.cards import ToolCard

if TYPE_CHECKING:
    from .subagents.manager import SubagentTaskSnapshot


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    text: str
    source: str
    important: bool = False


def summarize_tool_completion(
    card: ToolCard,
    *,
    metadata: dict[str, Any] | None = None,
) -> ProgressUpdate | None:
    tool_name = str(card.tool_name or "bash")
    if tool_name == "browser":
        return _summarize_browser(card, metadata or {})
    if tool_name == "holonet":
        return _summarize_holonet(card)
    if tool_name == "vision":
        return _summarize_vision(card)
    if tool_name in {"read_file", "write_file", "edit_file"}:
        return _summarize_file_tool(card)
    if tool_name == "verify":
        return _summarize_verify(card)
    if tool_name == "runbook":
        return _summarize_runbook(card)
    if tool_name == "bash":
        return _summarize_bash(card)
    return None


def summarize_subagent_completion(task: "SubagentTaskSnapshot") -> ProgressUpdate | None:
    label = task.name or task.task_id
    if task.status == "completed":
        summary = task.result_excerpt or "completed"
        return ProgressUpdate(
            text=f"subagent {label} finished: {summary}",
            source="subagent",
            important=True,
        )
    if task.status == "cancelled":
        return ProgressUpdate(
            text=f"subagent {label} cancelled",
            source="subagent",
            important=True,
        )
    return ProgressUpdate(
        text=f"subagent {label} failed: {task.error or 'unknown error'}",
        source="subagent",
        important=True,
    )


def combine_progress_updates(updates: list[ProgressUpdate]) -> str | None:
    if not updates:
        return None
    meaningful = [item for item in updates if item.text.strip()]
    if not meaningful:
        return None
    if len(meaningful) < 2 and not any(item.important for item in meaningful):
        return None
    texts = [item.text.strip().rstrip(".") for item in meaningful]
    if len(texts) > 3:
        head = "; ".join(texts[:3])
        return f"progress: {head}; +{len(texts) - 3} more"
    return "progress: " + "; ".join(texts)


def _summarize_browser(card: ToolCard, metadata: dict[str, Any]) -> ProgressUpdate | None:
    action = str(card.tool_arguments.get("action") or "").strip().lower() or card.verb
    target = _clip_target(
        str(card.tool_arguments.get("target") or card.tool_arguments.get("url") or ""),
    )
    controls_summary = str(metadata.get("controls_summary") or "").strip()
    visible_controls = _visible_control_count(controls_summary)
    intervention = metadata.get("verification_intervention")

    if intervention:
        kind = str(intervention.get("kind") or "").strip().lower()
        if kind == "repeat_failure":
            return ProgressUpdate(
                text="browser verification hit repeated failures",
                source="browser",
                important=True,
            )
        if kind == "repeated_open":
            return ProgressUpdate(
                text="browser reopened the same page state",
                source="browser",
                important=True,
            )
        if kind == "stagnant_state":
            return ProgressUpdate(
                text="browser actions stalled on the same page state",
                source="browser",
                important=True,
            )

    if action == "inspect":
        if visible_controls > 0:
            noun = "control" if visible_controls == 1 else "controls"
            return ProgressUpdate(
                text=f"inspected page controls ({visible_controls} visible {noun})",
                source="browser",
                important=True,
            )
        return ProgressUpdate(
            text="inspected current page state",
            source="browser",
            important=True,
        )
    if action == "open":
        label = target or "page"
        return ProgressUpdate(
            text=f"opened {label}",
            source="browser",
            important=True,
        )
    if action == "screenshot":
        return ProgressUpdate(
            text="captured browser screenshot",
            source="browser",
            important=True,
        )
    if action == "console_errors":
        if "No console errors" in card.output:
            return ProgressUpdate(
                text="checked browser console (clean)",
                source="browser",
                important=True,
            )
        return ProgressUpdate(
            text="checked browser console errors",
            source="browser",
            important=True,
        )
    if action == "clear_storage":
        scope = str(card.tool_arguments.get("scope") or "both").strip().lower() or "both"
        return ProgressUpdate(
            text=f"cleared browser {scope} storage",
            source="browser",
            important=True,
        )
    if action == "storage_state":
        return ProgressUpdate(
            text="inspected browser storage state",
            source="browser",
            important=True,
        )
    if action in {"click", "type", "press", "select", "wait_for", "extract_text"}:
        label = target or action
        return ProgressUpdate(
            text=f"browser {action} on {label}",
            source="browser",
            important=False,
        )
    return ProgressUpdate(
        text=f"browser {action}",
        source="browser",
        important=False,
    )


def _summarize_holonet(card: ToolCard) -> ProgressUpdate | None:
    provider = str(card.tool_arguments.get("provider") or card.tool_arguments.get("route") or "")
    query = str(card.tool_arguments.get("query") or "").strip()
    label = _clip_target(query, limit=56)
    if provider and label:
        return ProgressUpdate(
            text=f"{provider} search for {label}",
            source="holonet",
            important=True,
        )
    if provider:
        return ProgressUpdate(
            text=f"{provider} query completed",
            source="holonet",
            important=True,
        )
    return ProgressUpdate(
        text="web research step completed",
        source="holonet",
        important=True,
    )


def _summarize_vision(card: ToolCard) -> ProgressUpdate | None:
    path = str(card.tool_arguments.get("path") or "").strip()
    name = Path(os.path.expanduser(path)).name if path else "image"
    return ProgressUpdate(
        text=f"reviewed {name} with vision",
        source="vision",
        important=True,
    )


def _summarize_verify(card: ToolCard) -> ProgressUpdate | None:
    items = card.tool_arguments.get("items")
    if not isinstance(items, list):
        return ProgressUpdate(
            text="updated verification contract",
            source="verify",
            important=True,
        )
    passed = sum(
        1
        for item in items
        if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "passed"
    )
    failed = sum(
        1
        for item in items
        if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "failed"
    )
    in_progress = sum(
        1
        for item in items
        if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "in_progress"
    )
    total = len(items)
    if failed:
        return ProgressUpdate(
            text=f"verification contract updated ({passed}/{total} passed, {failed} failed)",
            source="verify",
            important=True,
        )
    if in_progress:
        return ProgressUpdate(
            text=f"verification contract updated ({passed}/{total} passed, {in_progress} running)",
            source="verify",
            important=True,
        )
    return ProgressUpdate(
        text=f"verification contract updated ({passed}/{total} passed)",
        source="verify",
        important=True,
    )


def _summarize_runbook(card: ToolCard) -> ProgressUpdate | None:
    args = card.tool_arguments
    if bool(args.get("clear")):
        return ProgressUpdate(
            text="cleared experiment runbook",
            source="runbook",
            important=True,
        )
    objective = str(args.get("objective") or "").strip()
    baseline_status = str(args.get("baseline_status") or "").strip().lower()
    attempt = args.get("attempt")
    label = _clip_target(objective or "current objective", limit=56)
    if isinstance(attempt, dict):
        decision = str(attempt.get("decision") or "").strip().lower() or "recorded"
        hypothesis = _clip_target(str(attempt.get("hypothesis") or ""), limit=44)
        return ProgressUpdate(
            text=f"runbook recorded {decision} attempt for {hypothesis or label}",
            source="runbook",
            important=True,
        )
    if baseline_status == "missing":
        return ProgressUpdate(
            text=f"runbook updated ({label}; baseline missing)",
            source="runbook",
            important=True,
        )
    return ProgressUpdate(
        text=f"runbook updated for {label}",
        source="runbook",
        important=True,
    )


def _summarize_bash(card: ToolCard) -> ProgressUpdate | None:
    failed = card.exit_code not in (None, 0)
    if card.change_artifact is not None and card.change_artifact.files:
        files = [
            file.path
            for file in card.change_artifact.files
            if file.path and file.path != "(patch)"
        ]
        if failed:
            if len(files) == 1:
                return ProgressUpdate(
                    text=f"bash command failed after touching {_clip_target(files[0])}",
                    source="bash",
                    important=True,
                )
            if len(files) > 1:
                return ProgressUpdate(
                    text=f"bash command failed after touching {len(files)} files",
                    source="bash",
                    important=True,
                )
        if len(files) == 1:
            return ProgressUpdate(
                text=f"updated {_clip_target(files[0])}",
                source="bash",
                important=True,
            )
        if len(files) > 1:
            return ProgressUpdate(
                text=f"updated {len(files)} files",
                source="bash",
                important=True,
            )
    path = _first_param(card, "path")
    if card.verb in {"write-file", "append-file", "copy-file", "move-file", "make-directory", "touch-file"}:
        label = path or card.raw_command or card.verb
        return ProgressUpdate(
            text=f"{card.verb.replace('-', ' ')} {_clip_target(label)}",
            source="bash",
            important=True,
        )
    if card.verb in {"git-diff", "git-show"}:
        return ProgressUpdate(
            text="reviewed code diff",
            source="bash",
            important=True,
        )
    if card.verb in {"read-file", "search-files", "list-directory"}:
        label = path or _first_param(card, "pattern") or card.verb
        return ProgressUpdate(
            text=f"{card.verb.replace('-', ' ')} {_clip_target(label)}",
            source="bash",
            important=False,
        )
    return None


def _summarize_file_tool(card: ToolCard) -> ProgressUpdate | None:
    path = str(card.tool_arguments.get("file_path") or _first_param(card, "path")).strip()
    label = _clip_target(path) or card.verb
    failed = card.exit_code not in (None, 0)

    if card.tool_name == "read_file":
        return ProgressUpdate(
            text=f"read file {label}",
            source="file",
            important=False,
        )

    if card.change_artifact is not None and card.change_artifact.files:
        files = [
            file.path
            for file in card.change_artifact.files
            if file.path and file.path != "(patch)"
        ]
        if failed:
            if len(files) == 1:
                return ProgressUpdate(
                    text=f"{card.tool_name.replace('_', ' ')} failed after touching {_clip_target(files[0])}",
                    source="file",
                    important=True,
                )
            if len(files) > 1:
                return ProgressUpdate(
                    text=f"{card.tool_name.replace('_', ' ')} failed after touching {len(files)} files",
                    source="file",
                    important=True,
                )
        if len(files) == 1:
            return ProgressUpdate(
                text=f"updated {_clip_target(files[0])}",
                source="file",
                important=True,
            )
        if len(files) > 1:
            return ProgressUpdate(
                text=f"updated {len(files)} files",
                source="file",
                important=True,
            )

    if failed:
        return ProgressUpdate(
            text=f"{card.tool_name.replace('_', ' ')} failed for {label}",
            source="file",
            important=True,
        )

    return ProgressUpdate(
        text=f"{card.tool_name.replace('_', ' ')} {label}",
        source="file",
        important=True,
    )


def _first_param(card: ToolCard, name: str) -> str:
    for key, value in card.params:
        if key == name:
            return str(value)
    return ""


def _visible_control_count(summary: str) -> int:
    if not summary:
        return 0
    return len(re.findall(r"^- ", summary, flags=re.MULTILINE))


def _clip_target(text: str, *, limit: int = 48) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"
