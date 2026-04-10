"""Session-local experimental runbook and attempt ledger helpers.

The task ledger tracks what work is active.
The verification contract tracks what must be proven.
The runbook tracks the broader experimental frame:

- what objective the run is pursuing
- what evaluator bundle should stay stable
- whether a baseline exists
- what the active hypothesis is
- what attempt decisions have already been recorded

This is the smallest Successor-native analogue to autoresearch's
`program.md` + `results.tsv` discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


RunbookStatus = Literal["planning", "running", "blocked", "complete"]
BaselineStatus = Literal["missing", "captured", "stale"]
EvaluatorKind = Literal[
    "command",
    "browser_flow",
    "vision_check",
    "script",
    "manual_probe",
]
AttemptDecision = Literal["kept", "discarded", "inconclusive", "failed_env"]

MAX_SCOPE_ITEMS = 12
MAX_PROTECTED_ITEMS = 12
MAX_EVALUATOR_STEPS = 8
MAX_ATTEMPT_FILES = 16
MAX_ATTEMPT_ARTIFACTS = 16


class RunbookError(ValueError):
    """Raised when a model-emitted runbook payload is invalid."""


def _normalize_required_text(value: Any, *, field_name: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise RunbookError(f"runbook.{field_name} must be a non-empty string")
    return text


def _normalize_optional_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_text_list(
    value: Any,
    *,
    field_name: str,
    max_items: int,
) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise RunbookError(f"runbook.{field_name} must be an array")
    if len(value) > max_items:
        raise RunbookError(
            f"runbook.{field_name} may contain at most {max_items} entries"
        )
    items: list[str] = []
    for idx, raw in enumerate(value, start=1):
        text = _normalize_required_text(raw, field_name=f"{field_name}[{idx}]")
        items.append(text)
    return tuple(items)


def _normalize_runbook_status(value: Any) -> RunbookStatus:
    status = str(value or "").strip().lower() or "planning"
    if status not in {"planning", "running", "blocked", "complete"}:
        raise RunbookError(
            "runbook.status must be one of: planning, running, blocked, complete"
        )
    return status  # type: ignore[return-value]


def _normalize_baseline_status(value: Any) -> BaselineStatus:
    status = str(value or "").strip().lower() or "missing"
    if status not in {"missing", "captured", "stale"}:
        raise RunbookError(
            "runbook.baseline_status must be one of: missing, captured, stale"
        )
    return status  # type: ignore[return-value]


def _normalize_evaluator_kind(value: Any) -> EvaluatorKind:
    kind = str(value or "").strip().lower()
    if kind not in {
        "command",
        "browser_flow",
        "vision_check",
        "script",
        "manual_probe",
    }:
        raise RunbookError(
            "runbook.evaluator.kind must be one of: command, browser_flow, vision_check, script, manual_probe"
        )
    return kind  # type: ignore[return-value]


def _normalize_attempt_decision(value: Any) -> AttemptDecision:
    decision = str(value or "").strip().lower()
    if decision not in {"kept", "discarded", "inconclusive", "failed_env"}:
        raise RunbookError(
            "runbook.attempt.decision must be one of: kept, discarded, inconclusive, failed_env"
        )
    return decision  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class EvaluatorStep:
    step_id: str
    kind: EvaluatorKind
    spec: str
    pass_condition: str


def parse_evaluator_steps(raw_steps: Any) -> tuple[EvaluatorStep, ...]:
    if raw_steps in (None, ""):
        return ()
    if not isinstance(raw_steps, list):
        raise RunbookError("runbook.evaluator must be an array")
    if len(raw_steps) > MAX_EVALUATOR_STEPS:
        raise RunbookError(
            f"runbook.evaluator may contain at most {MAX_EVALUATOR_STEPS} entries"
        )
    steps: list[EvaluatorStep] = []
    for idx, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise RunbookError(f"runbook.evaluator[{idx}] must be an object")
        steps.append(
            EvaluatorStep(
                step_id=_normalize_required_text(
                    raw_step.get("id"),
                    field_name=f"evaluator[{idx}].id",
                ),
                kind=_normalize_evaluator_kind(raw_step.get("kind")),
                spec=_normalize_required_text(
                    raw_step.get("spec"),
                    field_name=f"evaluator[{idx}].spec",
                ),
                pass_condition=_normalize_required_text(
                    raw_step.get("pass_condition"),
                    field_name=f"evaluator[{idx}].pass_condition",
                ),
            )
        )
    return tuple(steps)


@dataclass(frozen=True, slots=True)
class ExperimentAttempt:
    attempt_id: int
    hypothesis: str
    summary: str
    decision: AttemptDecision
    files_touched: tuple[str, ...] = ()
    evaluator_summary: str = ""
    verification_summary: str = ""
    artifact_refs: tuple[str, ...] = ()


def parse_experiment_attempt(
    raw_attempt: Any,
    *,
    next_attempt_id: int,
) -> ExperimentAttempt | None:
    if raw_attempt in (None, ""):
        return None
    if not isinstance(raw_attempt, dict):
        raise RunbookError("runbook.attempt must be an object")
    return ExperimentAttempt(
        attempt_id=next_attempt_id,
        hypothesis=_normalize_required_text(
            raw_attempt.get("hypothesis"),
            field_name="attempt.hypothesis",
        ),
        summary=_normalize_required_text(
            raw_attempt.get("summary"),
            field_name="attempt.summary",
        ),
        decision=_normalize_attempt_decision(raw_attempt.get("decision")),
        files_touched=_normalize_text_list(
            raw_attempt.get("files_touched"),
            field_name="attempt.files_touched",
            max_items=MAX_ATTEMPT_FILES,
        ),
        evaluator_summary=_normalize_optional_text(
            raw_attempt.get("evaluator_summary"),
        ),
        verification_summary=_normalize_optional_text(
            raw_attempt.get("verification_summary"),
        ),
        artifact_refs=_normalize_text_list(
            raw_attempt.get("artifact_refs"),
            field_name="attempt.artifact_refs",
            max_items=MAX_ATTEMPT_ARTIFACTS,
        ),
    )


@dataclass(frozen=True, slots=True)
class SessionRunbookState:
    objective: str
    success_definition: str
    scope: tuple[str, ...] = ()
    protected_surfaces: tuple[str, ...] = ()
    baseline_status: BaselineStatus = "missing"
    baseline_summary: str = ""
    active_hypothesis: str = ""
    evaluator: tuple[EvaluatorStep, ...] = ()
    decision_policy: str = ""
    status: RunbookStatus = "planning"

    def is_configured(self) -> bool:
        return bool(self.objective)

    def baseline_missing(self) -> bool:
        return self.baseline_status == "missing"


@dataclass(slots=True)
class SessionRunbook:
    state: SessionRunbookState | None = None

    def replace(self, state: SessionRunbookState | None) -> None:
        self.state = state

    def clear(self) -> None:
        self.state = None

    def has_state(self) -> bool:
        return self.state is not None and self.state.is_configured()

    def active_hypothesis(self) -> str:
        if self.state is None:
            return ""
        return self.state.active_hypothesis


def parse_runbook_state(arguments: dict[str, Any]) -> SessionRunbookState | None:
    if bool(arguments.get("clear")):
        return None
    objective = _normalize_required_text(
        arguments.get("objective"),
        field_name="objective",
    )
    success_definition = _normalize_required_text(
        arguments.get("success_definition"),
        field_name="success_definition",
    )
    return SessionRunbookState(
        objective=objective,
        success_definition=success_definition,
        scope=_normalize_text_list(
            arguments.get("scope"),
            field_name="scope",
            max_items=MAX_SCOPE_ITEMS,
        ),
        protected_surfaces=_normalize_text_list(
            arguments.get("protected_surfaces"),
            field_name="protected_surfaces",
            max_items=MAX_PROTECTED_ITEMS,
        ),
        baseline_status=_normalize_baseline_status(arguments.get("baseline_status")),
        baseline_summary=_normalize_optional_text(arguments.get("baseline_summary")),
        active_hypothesis=_normalize_optional_text(arguments.get("active_hypothesis")),
        evaluator=parse_evaluator_steps(arguments.get("evaluator")),
        decision_policy=_normalize_optional_text(arguments.get("decision_policy")),
        status=_normalize_runbook_status(arguments.get("status")),
    )


def runbook_state_to_payload(state: SessionRunbookState | None) -> dict[str, object]:
    if state is None:
        return {"clear": True}
    return {
        "objective": state.objective,
        "success_definition": state.success_definition,
        "scope": list(state.scope),
        "protected_surfaces": list(state.protected_surfaces),
        "baseline_status": state.baseline_status,
        "baseline_summary": state.baseline_summary,
        "active_hypothesis": state.active_hypothesis,
        "evaluator": evaluator_steps_to_payload(state.evaluator),
        "decision_policy": state.decision_policy,
        "status": state.status,
    }


def evaluator_steps_to_payload(
    steps: tuple[EvaluatorStep, ...],
) -> list[dict[str, str]]:
    return [
        {
            "id": step.step_id,
            "kind": step.kind,
            "spec": step.spec,
            "pass_condition": step.pass_condition,
        }
        for step in steps
    ]


def experiment_attempt_to_payload(
    attempt: ExperimentAttempt,
) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "hypothesis": attempt.hypothesis,
        "summary": attempt.summary,
        "decision": attempt.decision,
        "files_touched": list(attempt.files_touched),
        "evaluator_summary": attempt.evaluator_summary,
        "verification_summary": attempt.verification_summary,
        "artifact_refs": list(attempt.artifact_refs),
    }


def build_runbook_card_output(
    state: SessionRunbookState | None,
    *,
    attempt: ExperimentAttempt | None = None,
) -> str:
    if state is None:
        return "Cleared the session runbook."
    lines = ["Updated the session runbook."]
    lines.append(f"- objective: {state.objective}")
    lines.append(f"- status: {state.status}")
    lines.append(f"- baseline: {state.baseline_status}")
    if state.baseline_summary:
        lines.append(f"  baseline summary: {state.baseline_summary}")
    if state.active_hypothesis:
        lines.append(f"- active hypothesis: {state.active_hypothesis}")
    if state.evaluator:
        lines.append(f"- evaluator steps: {len(state.evaluator)}")
        for step in state.evaluator[:4]:
            lines.append(f"  - {step.step_id} [{step.kind}]")
    if attempt is not None:
        lines.append(
            f"- recorded attempt {attempt.attempt_id} [{attempt.decision}]: {attempt.hypothesis}"
        )
        lines.append(f"  summary: {attempt.summary}")
    return "\n".join(lines)


def build_runbook_tool_result(
    state: SessionRunbookState | None,
    *,
    attempt: ExperimentAttempt | None = None,
) -> str:
    if state is None:
        return "<runbook><cleared>true</cleared></runbook>"
    lines = [
        "<runbook>",
        f"<status>{state.status}</status>",
        f"<objective>{state.objective}</objective>",
        f"<success-definition>{state.success_definition}</success-definition>",
        f"<baseline-status>{state.baseline_status}</baseline-status>",
        f"<baseline-summary>{state.baseline_summary}</baseline-summary>",
        f"<active-hypothesis>{state.active_hypothesis}</active-hypothesis>",
        f"<decision-policy>{state.decision_policy}</decision-policy>",
    ]
    for item in state.scope:
        lines.append(f"<scope>{item}</scope>")
    for item in state.protected_surfaces:
        lines.append(f"<protected>{item}</protected>")
    for step in state.evaluator:
        lines.extend(
            [
                "<evaluator-step>",
                f"<id>{step.step_id}</id>",
                f"<kind>{step.kind}</kind>",
                f"<spec>{step.spec}</spec>",
                f"<pass-condition>{step.pass_condition}</pass-condition>",
                "</evaluator-step>",
            ]
        )
    if attempt is not None:
        lines.extend(
            [
                "<latest-attempt>",
                f"<attempt-id>{attempt.attempt_id}</attempt-id>",
                f"<decision>{attempt.decision}</decision>",
                f"<hypothesis>{attempt.hypothesis}</hypothesis>",
                f"<summary>{attempt.summary}</summary>",
                "</latest-attempt>",
            ]
        )
    lines.append("</runbook>")
    return "\n".join(lines)


def build_runbook_prompt_section(runbook: SessionRunbook) -> str:
    lines = ["## Current Runbook", ""]
    state = runbook.state
    if state is None:
        lines.append("No current runbook.")
        return "\n".join(lines)
    lines.append(f"- objective: {state.objective}")
    lines.append(f"- success: {state.success_definition}")
    lines.append(f"- status: {state.status}")
    lines.append(f"- baseline: {state.baseline_status}")
    if state.baseline_summary:
        lines.append(f"  summary: {state.baseline_summary}")
    if state.active_hypothesis:
        lines.append(f"- active hypothesis: {state.active_hypothesis}")
    if state.scope:
        lines.append("- scope:")
        for item in state.scope:
            lines.append(f"  - {item}")
    if state.protected_surfaces:
        lines.append("- protected:")
        for item in state.protected_surfaces:
            lines.append(f"  - {item}")
    if state.evaluator:
        lines.append("- evaluator:")
        for step in state.evaluator:
            lines.append(
                f"  - {step.step_id} [{step.kind}] {step.spec} -> {step.pass_condition}"
            )
    return "\n".join(lines)


def build_runbook_execution_guidance(runbook: SessionRunbook) -> str:
    lines = ["### Experimental run discipline", ""]
    state = runbook.state
    if state is None:
        lines.extend([
            "- For long iterative implementation work, create a runbook early with objective, evaluator steps, baseline status, and one active hypothesis.",
            "- A runbook is especially useful when the work will involve multiple edit -> run -> verify loops.",
        ])
    else:
        if state.baseline_missing():
            lines.append(
                "- The current runbook says the baseline is `missing`. Capture baseline evidence before major new edits."
            )
        elif state.baseline_status == "stale":
            lines.append(
                "- The current runbook says the baseline is `stale`. Refresh it before trusting new comparisons."
            )
        if state.active_hypothesis:
            lines.append(
                f"- The active hypothesis is `{state.active_hypothesis}`. Prefer bounded work that directly tests it."
            )
        else:
            lines.append(
                "- The runbook has no active hypothesis. Set one before the next major attempt."
            )
        if state.evaluator:
            lines.append(
                "- Reuse the existing evaluator steps instead of inventing new ad hoc checks unless the task genuinely changed."
            )
    lines.extend([
        "- After a bounded cluster of edits, run the evaluator before continuing with more speculative changes.",
        "- Record a concise attempt entry after meaningful evaluator results: hypothesis, outcome, and keep/discard decision.",
        "- Prefer append-only experiment memory over retrying failed ideas blindly.",
        "- Do not mark a hypothesis as successful from source inspection alone; use evaluator and verification evidence.",
    ])
    return "\n".join(lines)


def build_runbook_artifact(
    state: SessionRunbookState | None,
    *,
    attempt_count: int = 0,
    last_attempt: ExperimentAttempt | None = None,
) -> dict[str, object]:
    if state is None:
        return {
            "configured": False,
            "attempt_count": attempt_count,
        }
    return {
        "configured": True,
        "objective": state.objective,
        "success_definition": state.success_definition,
        "scope": list(state.scope),
        "protected_surfaces": list(state.protected_surfaces),
        "baseline_status": state.baseline_status,
        "baseline_summary": state.baseline_summary,
        "active_hypothesis": state.active_hypothesis,
        "status": state.status,
        "decision_policy": state.decision_policy,
        "evaluator": evaluator_steps_to_payload(state.evaluator),
        "attempt_count": attempt_count,
        "last_attempt": experiment_attempt_to_payload(last_attempt)
        if last_attempt is not None
        else None,
    }
