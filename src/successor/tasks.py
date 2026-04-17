"""Session-local task ledger for structured long-run autonomy.

The ledger is intentionally explicit:

- session-local only; never persisted to profile/config
- compact enough to fit comfortably in the system prompt
- strict enough that the runtime can make one narrow continuation
  decision based on `in_progress` state

This is the closest analogue to free-code's todo/task path that fits
Successor's current architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TaskStatus = Literal["pending", "in_progress", "completed"]
MAX_TASKS = 64


class TaskLedgerError(ValueError):
    """Raised when a model-emitted task payload is structurally invalid."""


def _normalize_text(value: Any, *, field_name: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise TaskLedgerError(f"task.{field_name} must be a non-empty string")
    return text


def _normalize_status(value: Any) -> TaskStatus:
    status = str(value or "").strip().lower()
    if status not in {"pending", "in_progress", "completed"}:
        raise TaskLedgerError(
            "task.status must be one of: pending, in_progress, completed"
        )
    return status  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class SessionTask:
    content: str
    active_form: str
    status: TaskStatus

    @property
    def done(self) -> bool:
        return self.status == "completed"

    @property
    def in_progress(self) -> bool:
        return self.status == "in_progress"


def parse_task_items(raw_items: Any) -> tuple[SessionTask, ...]:
    """Validate and normalize a tool payload into immutable tasks."""
    if raw_items is None:
        raise TaskLedgerError("task.items is required")
    if not isinstance(raw_items, list):
        raise TaskLedgerError("task.items must be an array")
    if len(raw_items) > MAX_TASKS:
        raise TaskLedgerError(f"task.items may contain at most {MAX_TASKS} entries")

    items: list[SessionTask] = []
    in_progress_count = 0
    for idx, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise TaskLedgerError(f"task.items[{idx}] must be an object")
        content = _normalize_text(raw_item.get("content"), field_name="content")
        active_raw = raw_item.get("active_form")
        active_form = content if active_raw in (None, "") else _normalize_text(
            active_raw,
            field_name="active_form",
        )
        status = _normalize_status(raw_item.get("status"))
        if status == "in_progress":
            in_progress_count += 1
        items.append(SessionTask(content=content, active_form=active_form, status=status))

    if in_progress_count > 1:
        raise TaskLedgerError("task.items may contain at most one in_progress task")
    return tuple(items)


@dataclass(slots=True)
class SessionTaskLedger:
    items: tuple[SessionTask, ...] = field(default_factory=tuple)

    def replace(self, items: tuple[SessionTask, ...]) -> None:
        self.items = tuple(items)

    def clear(self) -> None:
        self.items = ()

    def has_items(self) -> bool:
        return bool(self.items)

    def has_in_progress(self) -> bool:
        return any(item.in_progress for item in self.items)

    def open_count(self) -> int:
        return sum(1 for item in self.items if not item.done)

    def completed_count(self) -> int:
        return sum(1 for item in self.items if item.done)

    def in_progress_task(self) -> SessionTask | None:
        for item in self.items:
            if item.in_progress:
                return item
        return None


def task_items_to_payload(items: tuple[SessionTask, ...]) -> list[dict[str, str]]:
    return [
        {
            "content": item.content,
            "active_form": item.active_form,
            "status": item.status,
        }
        for item in items
    ]


def build_task_card_output(ledger: SessionTaskLedger) -> str:
    if not ledger.items:
        return "Cleared the session task ledger."
    lines = ["Updated the session task ledger."]
    for item in ledger.items:
        label = {
            "pending": "pending",
            "in_progress": "in progress",
            "completed": "completed",
        }[item.status]
        lines.append(f"- [{label}] {item.content}")
        if item.in_progress and item.active_form != item.content:
            lines.append(f"  active: {item.active_form}")
    return "\n".join(lines)


def build_task_tool_result(ledger: SessionTaskLedger) -> str:
    lines = [
        "<task-ledger>",
        f"<task-count>{len(ledger.items)}</task-count>",
    ]
    active = ledger.in_progress_task()
    if active is not None:
        lines.append(f"<active-task>{active.active_form}</active-task>")
    for item in ledger.items:
        lines.extend(
            [
                "<task>",
                f"<status>{item.status}</status>",
                f"<content>{item.content}</content>",
                f"<active-form>{item.active_form}</active-form>",
                "</task>",
            ]
        )
    lines.append("</task-ledger>")
    return "\n".join(lines)


def build_task_prompt_section(ledger: SessionTaskLedger) -> str:
    lines = ["## Current Session Tasks", ""]
    if not ledger.items:
        lines.append("No current task ledger.")
        return "\n".join(lines)
    for item in ledger.items:
        lines.append(f"- [{item.status}] {item.content}")
        if item.in_progress and item.active_form != item.content:
            lines.append(f"  active: {item.active_form}")
    return "\n".join(lines)


def build_task_execution_primer() -> str:
    lines = ["### Task-ledger discipline", ""]
    lines.extend([
        "- For multi-step work, create or update the session task ledger before the first substantive mutation, process-management step, or verification loop.",
        "- If the request clearly requires 3 or more distinct actions, includes build + verify + fix phases, or will span several turns, a `task` call is usually the first substantive step after you understand the request.",
        "- Mark one task `in_progress` BEFORE you begin substantive work, and keep at most one task `in_progress` while actively working.",
        "- Do not jump straight into a large `write_file` payload, multi-command bash sequence, or repeated browser flow before the ledger exists.",
        "- If you already know the next substantive tool action, update the ledger and make that tool call in the SAME response.",
        "- Mark tasks completed immediately after finishing them; do not batch completions later.",
        "- Skip the task ledger only for single trivial tasks or purely conversational replies.",
    ])
    return "\n".join(lines)


def build_task_execution_guidance(ledger: SessionTaskLedger) -> str:
    if not ledger.items:
        return build_task_execution_primer()
    lines = ["### Task-ledger discipline", ""]
    active = ledger.in_progress_task()
    if active is not None:
        lines.append(
            f"- A session task is already `in_progress`: `{active.active_form}`. Keep the ledger authoritative as you continue from the current state."
        )
    else:
        lines.append(
            "- A session task ledger already exists. Update it before you resume substantive work so it reflects the current plan."
        )
    lines.extend([
        "- Mark one task `in_progress` BEFORE you begin substantive work, and keep at most one task `in_progress` while actively working.",
        "- Do not jump straight into a large `write_file` payload, multi-command bash sequence, or repeated browser flow before the ledger exists.",
        "- If you already know the next substantive tool action, update the ledger and make that tool call in the SAME response.",
        "- Mark tasks completed immediately after finishing them; do not batch completions later.",
        "- Skip the task ledger only for single trivial tasks or purely conversational replies.",
    ])
    return "\n".join(lines)


def build_task_continue_nudge(ledger: SessionTaskLedger) -> str:
    active = ledger.in_progress_task()
    if active is None:
        return ""
    return (
        "A session task is still marked `in_progress`: "
        f"`{active.active_form}`. If you are still actively working, continue "
        "from the current state instead of handing control back yet. If the "
        "work is actually complete or you need user input, call the `task` "
        "tool first so the ledger reflects that before you stop."
    )
