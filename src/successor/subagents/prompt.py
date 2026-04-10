"""Prompt and notification helpers for background subagents."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import SubagentTaskSnapshot


_WORKER_BOILERPLATE = """<subagent-directive>
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

_VERIFICATION_BOILERPLATE = """<verification-directive>
STOP. READ THIS FIRST.

You are a background verification subagent running inside successor.
Your job is not to confirm the work by inspection. Your job is to
verify it with evidence and try to catch what the implementation path
missed.

Rules:
1. Read-only verification only. Do not modify project files.
2. Do not use `write_file`, `edit_file`, or spawn more subagents.
3. Use runtime evidence over source inspection whenever possible: run
   the command, hit the endpoint, inspect the page, check the output.
4. Start with the repo's documented build, test, lint, and type-check
   commands when they exist. Then verify the specific behavior directly.
5. Run at least one edge-case, failure-path, or adversarial check for
   non-trivial work.
6. If a relevant check could not be run, say exactly why.
7. Keep the final report concise, factual, and evidence-backed.
8. Your final response must begin with "Scope:" and include a
   "Checks:" line, a "Verdict:" line, and "Key files:" when relevant.
   Verdict must be one of PASS, FAIL, or PARTIAL.
</verification-directive>
"""


def normalize_subagent_role(role: str | None) -> str:
    cleaned = str(role or "worker").strip().lower()
    if cleaned in {"verification", "verifier", "verify"}:
        return "verification"
    return "worker"


def subagent_kind_label(role: str | None) -> str:
    return "verifier" if normalize_subagent_role(role) == "verification" else "subagent"


def build_child_prompt(directive: str, *, role: str = "worker") -> str:
    """Wrap a child directive with the correct boilerplate."""
    cleaned = directive.strip()
    boilerplate = (
        _VERIFICATION_BOILERPLATE
        if normalize_subagent_role(role) == "verification"
        else _WORKER_BOILERPLATE
    )
    return f"{boilerplate}\n\n{cleaned}"


def build_spawn_result_payload(task: SubagentTaskSnapshot) -> str:
    """Structured tool-result content for a newly spawned worker."""
    label = task.name or task.task_id
    kind = subagent_kind_label(task.role)
    return (
        "<subagent-spawned>\n"
        f"<task_id>{task.task_id}</task_id>\n"
        f"<name>{label}</name>\n"
        f"<role>{task.role}</role>\n"
        "<status>queued</status>\n"
        f"<note>Background {kind} started. Do not assume its findings. "
        "Wait for a later subagent notification before using the result.</note>\n"
        "</subagent-spawned>"
    )


def build_spawn_result_display(task: SubagentTaskSnapshot) -> str:
    """Human-facing display text for a newly spawned worker."""
    label = f" {task.name}" if task.name else ""
    kind = subagent_kind_label(task.role)
    return (
        f"spawned background {kind} {task.task_id}{label}. "
        "It will report back through a later task notification."
    )


def build_notification_payload(task: SubagentTaskSnapshot) -> str:
    """Structured notification text injected back into parent context."""
    label = task.name or task.task_id
    kind = subagent_kind_label(task.role)
    if task.status == "completed":
        result = task.result_text or task.result_excerpt or "completed"
        return (
            "<subagent-notification>\n"
            f"<task_id>{task.task_id}</task_id>\n"
            f"<name>{label}</name>\n"
            f"<role>{task.role}</role>\n"
            "<status>completed</status>\n"
            f"<kind>{kind}</kind>\n"
            f"<result>{result}</result>\n"
            f"<transcript>{task.transcript_path}</transcript>\n"
            "</subagent-notification>"
        )
    if task.status == "cancelled":
        return (
            "<subagent-notification>\n"
            f"<task_id>{task.task_id}</task_id>\n"
            f"<name>{label}</name>\n"
            f"<role>{task.role}</role>\n"
            "<status>cancelled</status>\n"
            f"<kind>{kind}</kind>\n"
            f"<error>{task.error or 'cancelled'}</error>\n"
            f"<transcript>{task.transcript_path}</transcript>\n"
            "</subagent-notification>"
        )
    return (
        "<subagent-notification>\n"
        f"<task_id>{task.task_id}</task_id>\n"
        f"<name>{label}</name>\n"
        f"<role>{task.role}</role>\n"
        "<status>failed</status>\n"
        f"<kind>{kind}</kind>\n"
        f"<error>{task.error or 'unknown error'}</error>\n"
        f"<transcript>{task.transcript_path}</transcript>\n"
        "</subagent-notification>"
    )


def build_notification_display(task: SubagentTaskSnapshot) -> str:
    """Human-facing notification shown in the chat transcript."""
    label = task.name or task.task_id
    kind = subagent_kind_label(task.role)
    if task.status == "completed":
        summary = task.result_excerpt or "completed"
        return (
            f"{kind} {label} ({task.task_id}) complete. "
            f"{summary} Transcript: {task.transcript_path}"
        )
    if task.status == "cancelled":
        return (
            f"{kind} {label} ({task.task_id}) cancelled. "
            f"Transcript: {task.transcript_path}"
        )
    return (
        f"{kind} {label} ({task.task_id}) failed: "
        f"{task.error or 'unknown error'}. Transcript: {task.transcript_path}"
    )
