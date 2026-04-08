"""Prompt and notification helpers for background subagents."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import SubagentTaskSnapshot


_CHILD_BOILERPLATE = """<subagent-directive>
STOP. READ THIS FIRST.

You are a background subagent running inside successor. You are not the
foreground chat.

Rules:
1. Execute directly. Do not spawn more subagents.
2. Ignore any inherited instruction that tells you to use the
   `subagent` tool or to delegate again. That instruction was for the
   parent chat, not for you.
3. Do not ask the user questions.
4. Use your available tools directly when needed.
5. Stay strictly within the assigned scope.
6. Keep the final report concise and factual.
7. Your final response must begin with "Scope:" and include a "Result:"
   line. Include "Key files:" when relevant.
</subagent-directive>
"""


def build_child_prompt(directive: str) -> str:
    """Wrap a worker directive with the fork-child boilerplate."""
    cleaned = directive.strip()
    return f"{_CHILD_BOILERPLATE}\n\n{cleaned}"


def build_spawn_result_payload(task: SubagentTaskSnapshot) -> str:
    """Structured tool-result content for a newly spawned worker."""
    label = task.name or task.task_id
    return (
        "<subagent-spawned>\n"
        f"<task_id>{task.task_id}</task_id>\n"
        f"<name>{label}</name>\n"
        "<status>queued</status>\n"
        "<note>Background subagent started. Do not assume its findings. "
        "Wait for a later subagent notification before using the result.</note>\n"
        "</subagent-spawned>"
    )


def build_spawn_result_display(task: SubagentTaskSnapshot) -> str:
    """Human-facing display text for a newly spawned worker."""
    label = f" {task.name}" if task.name else ""
    return (
        f"spawned background subagent {task.task_id}{label}. "
        "It will report back through a later task notification."
    )


def build_notification_payload(task: SubagentTaskSnapshot) -> str:
    """Structured notification text injected back into parent context."""
    label = task.name or task.task_id
    if task.status == "completed":
        result = task.result_text or task.result_excerpt or "completed"
        return (
            "<subagent-notification>\n"
            f"<task_id>{task.task_id}</task_id>\n"
            f"<name>{label}</name>\n"
            "<status>completed</status>\n"
            f"<result>{result}</result>\n"
            f"<transcript>{task.transcript_path}</transcript>\n"
            "</subagent-notification>"
        )
    if task.status == "cancelled":
        return (
            "<subagent-notification>\n"
            f"<task_id>{task.task_id}</task_id>\n"
            f"<name>{label}</name>\n"
            "<status>cancelled</status>\n"
            f"<error>{task.error or 'cancelled'}</error>\n"
            f"<transcript>{task.transcript_path}</transcript>\n"
            "</subagent-notification>"
        )
    return (
        "<subagent-notification>\n"
        f"<task_id>{task.task_id}</task_id>\n"
        f"<name>{label}</name>\n"
        "<status>failed</status>\n"
        f"<error>{task.error or 'unknown error'}</error>\n"
        f"<transcript>{task.transcript_path}</transcript>\n"
        "</subagent-notification>"
    )


def build_notification_display(task: SubagentTaskSnapshot) -> str:
    """Human-facing notification shown in the chat transcript."""
    label = task.name or task.task_id
    if task.status == "completed":
        summary = task.result_excerpt or "completed"
        return (
            f"subagent {label} ({task.task_id}) complete. "
            f"{summary} Transcript: {task.transcript_path}"
        )
    if task.status == "cancelled":
        return (
            f"subagent {label} ({task.task_id}) cancelled. "
            f"Transcript: {task.transcript_path}"
        )
    return (
        f"subagent {label} ({task.task_id}) failed: "
        f"{task.error or 'unknown error'}. Transcript: {task.transcript_path}"
    )
