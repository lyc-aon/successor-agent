"""Helpers for nudging task/runbook setup earlier on long scoped work."""

from __future__ import annotations

from dataclasses import dataclass

from .runbook import SessionRunbook
from .tasks import SessionTaskLedger
from .verification_adoption import (
    SubmissionActivity,
    looks_stateful_runtime_task,
    summarize_submission_activity,
)


_BUILD_KEYWORDS = (
    "build",
    "create",
    "make",
    "implement",
    "rewrite",
    "ship",
)

_VERIFICATION_KEYWORDS = (
    "verify",
    "verification",
    "test",
    "runtime",
    "browser",
    "visual",
    "e2e",
    "end to end",
    "screenshot",
    "observe",
    "debug",
)

_ITERATION_KEYWORDS = (
    "iterate",
    "iterative",
    "polish",
    "fix",
    "bug",
    "research",
    "analyze",
    "compare",
    "script",
    "driver",
    "autoplay",
)

_EXPLICIT_LONG_HORIZON_KEYWORDS = (
    "multi-step",
    "multi step",
    "multi-turn",
    "multi turn",
    "step by step",
    "several turns",
    "long task",
    "supervise",
    "supervision",
    "record",
    "recording",
    "playback",
    "thoroughly",
    "every step",
)

_RUNBOOK_HINT_KEYWORDS = (
    "baseline",
    "hypothesis",
    "evaluator",
    "compare",
    "retry",
    "loop",
    "iterate",
    "polish",
    "debug",
)


@dataclass(frozen=True, slots=True)
class TaskAdoptionDecision:
    should_nudge: bool
    kind: str = ""
    long_horizon: bool = False
    stateful_runtime: bool = False
    recommend_runbook: bool = False
    activity: SubmissionActivity = SubmissionActivity()
    text: str = ""


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def looks_long_horizon_task(*texts: str) -> bool:
    """Return True when the request clearly spans multiple phases."""
    for raw in texts:
        text = _normalize(raw)
        if not text:
            continue
        category_hits = sum(
            1
            for keywords in (
                _BUILD_KEYWORDS,
                _VERIFICATION_KEYWORDS,
                _ITERATION_KEYWORDS,
            )
            if _has_any(text, keywords)
        )
        explicit = _has_any(text, _EXPLICIT_LONG_HORIZON_KEYWORDS)
        if category_hits >= 3:
            return True
        if category_hits >= 2 and (explicit or len(text) >= 120):
            return True
        if explicit and category_hits >= 1:
            return True
    return False


def maybe_build_task_adoption_nudge(
    *,
    latest_user_text: str,
    active_task_text: str,
    ledger: SessionTaskLedger,
    runbook: SessionRunbook,
    messages: list[object],
) -> TaskAdoptionDecision:
    """Return a bounded reminder when long scoped work lacks setup."""
    long_horizon = looks_long_horizon_task(latest_user_text, active_task_text)
    stateful_runtime = looks_stateful_runtime_task(
        latest_user_text,
        active_task_text,
    )
    activity = summarize_submission_activity(messages)
    combined = _normalize(f"{latest_user_text}\n{active_task_text}")
    if (
        not long_horizon
        and stateful_runtime
        and _has_any(combined, _BUILD_KEYWORDS)
        and _has_any(combined, _VERIFICATION_KEYWORDS)
    ):
        long_horizon = True
    recommend_runbook = stateful_runtime or _has_any(combined, _RUNBOOK_HINT_KEYWORDS)

    if not long_horizon:
        return TaskAdoptionDecision(
            should_nudge=False,
            long_horizon=False,
            stateful_runtime=stateful_runtime,
            recommend_runbook=recommend_runbook,
            activity=activity,
        )

    if not ledger.has_items():
        if activity.substantive_actions >= 1:
            text = (
                "Long multi-phase work is already underway without a session "
                "task ledger. Call `task` now with 3-6 coarse steps before "
                "more edits, process work, or verification."
            )
            kind = "missing_ledger_after_work"
        else:
            text = (
                "This request clearly spans multiple phases. Before the first "
                "substantive mutation, call `task` with 3-6 coarse steps that "
                "cover the main build, verify, and finish phases."
            )
            kind = "missing_ledger_preflight"
        if recommend_runbook:
            text += (
                " Because this will likely involve repeated edit -> run -> "
                "verify loops, initialize `runbook` early so the objective, "
                "baseline, active hypothesis, and evaluator bundle stay stable."
            )
        if stateful_runtime:
            text += (
                " Because runtime behavior matters here, set up `verify` early "
                "with concrete proof items instead of relying on casual manual "
                "inspection later."
            )
        return TaskAdoptionDecision(
            should_nudge=True,
            kind=kind,
            long_horizon=True,
            stateful_runtime=stateful_runtime,
            recommend_runbook=recommend_runbook,
            activity=activity,
            text=text,
        )

    if recommend_runbook and not runbook.has_state() and activity.substantive_actions >= 2:
        return TaskAdoptionDecision(
            should_nudge=True,
            kind="missing_runbook",
            long_horizon=True,
            stateful_runtime=stateful_runtime,
            recommend_runbook=True,
            activity=activity,
            text=(
                "This run already has a task ledger, but it still lacks a "
                "`runbook`. Initialize `runbook` now so the objective, "
                "baseline, active hypothesis, and evaluator bundle stay "
                "stable across iterations."
            ),
        )

    return TaskAdoptionDecision(
        should_nudge=False,
        long_horizon=True,
        stateful_runtime=stateful_runtime,
        recommend_runbook=recommend_runbook,
        activity=activity,
    )
