"""Browser-verification control helpers.

The browser tool already exposes enough page-state metadata to detect
when the model is thrashing instead of verifying. This module keeps the
policy small and deterministic:

- classify whether the current task looks verification-shaped
- track repeated browser failures / no-op state loops inside one browser session
- attach structured intervention metadata to browser tool results

The chat loop can then decide when those interventions should become
continuation nudges, progress summaries, and trace events.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..tool_runner import ToolExecutionResult


_VERIFICATION_KEYWORDS = (
    "verify",
    "verification",
    "check",
    "inspect",
    "look for",
    "look out for",
    "polish",
    "qa",
    "quality assurance",
    "reproduce",
    "repro",
    "debug",
    "bug",
    "issue",
    "regression",
    "visual",
    "layout",
    "runtime",
    "console",
    "human",
    "smoke test",
    "test like a human",
)


def classify_browser_verification(
    *,
    latest_user_text: str,
    active_task_text: str = "",
    browser_verifier_loaded: bool = False,
) -> tuple[bool, str]:
    """Return `(active, reason)` for browser-verification mode."""
    if browser_verifier_loaded:
        return True, "skill"
    haystacks = [
        ("user", latest_user_text),
        ("task", active_task_text),
    ]
    for source, text in haystacks:
        lowered = " ".join(text.lower().split())
        if not lowered:
            continue
        if any(keyword in lowered for keyword in _VERIFICATION_KEYWORDS):
            return True, source
    return False, ""


@dataclass(slots=True)
class BrowserProgressTracker:
    """Track repeated failures and no-op browser actions within one session."""

    last_action: str = ""
    last_target: str = ""
    last_state_hash: str = ""
    stagnant_repeats: int = 0
    last_failure_signature: tuple[str, str] | None = None
    repeat_failures: int = 0
    last_controls_summary: str = ""

    def annotate(
        self,
        arguments: dict[str, Any],
        result: ToolExecutionResult,
    ) -> ToolExecutionResult:
        action = str(arguments.get("action", "") or "").strip().lower()
        target = str(arguments.get("target", "") or arguments.get("url", "") or "").strip()
        metadata = dict(result.metadata or {})
        state_hash = str(metadata.get("state_hash", "") or "")
        controls_summary = str(metadata.get("controls_summary", "") or "").strip()
        if controls_summary:
            self.last_controls_summary = controls_summary

        intervention: dict[str, Any] | None = None
        output = result.output or ""
        stderr = result.stderr or ""

        if result.exit_code != 0:
            signature = (action, target)
            if signature == self.last_failure_signature:
                self.repeat_failures += 1
            else:
                self.repeat_failures = 1
                self.last_failure_signature = signature
            if self.repeat_failures >= 2:
                note = (
                    "Progress note: this browser action has failed repeatedly. "
                    "Stop retrying the same step. Call `inspect` to list the "
                    "actual visible controls and selector hints, or switch strategy."
                )
                if self.last_controls_summary:
                    note = f"{note}\n\n{self.last_controls_summary}"
                stderr = _append_note(stderr, note)
                intervention = {
                    "kind": "repeat_failure",
                    "note": note,
                    "repeat_failures": self.repeat_failures,
                    "recommended_action": "inspect",
                    "controls_summary": self.last_controls_summary,
                }
            return self._finalize_result(
                result,
                metadata=metadata,
                action=action,
                target=target,
                state_hash=state_hash,
                repeated_open=False,
                output=output,
                stderr=stderr,
                intervention=intervention,
            )

        self.repeat_failures = 0
        self.last_failure_signature = None

        repeated_open = (
            action == "open"
            and state_hash
            and self.last_action == "open"
            and target == self.last_target
            and state_hash == self.last_state_hash
        )
        if repeated_open:
            note = (
                "Progress note: you reopened the same page and got the same "
                "state back. Reuse the current browser session unless a code "
                "edit, storage reset, or explicit reload is actually required."
            )
            if controls_summary:
                note = f"{note}\n\n{controls_summary}"
            output = _append_note(output, note)
            intervention = {
                "kind": "repeated_open",
                "note": note,
                "recommended_action": "reuse_session",
                "controls_summary": controls_summary,
            }

        self.last_action = action
        self.last_target = target

        if action not in {"click", "type", "press", "select", "wait_for"} or not state_hash:
            if state_hash:
                self.last_state_hash = state_hash
            self.stagnant_repeats = 0
            return self._finalize_result(
                result,
                metadata=metadata,
                action=action,
                target=target,
                state_hash=state_hash,
                repeated_open=repeated_open,
                output=output,
                stderr=stderr,
                intervention=intervention,
            )

        if state_hash == self.last_state_hash:
            self.stagnant_repeats += 1
        else:
            self.stagnant_repeats = 0
        self.last_state_hash = state_hash

        if self.stagnant_repeats >= 2:
            note = (
                "Progress note: page state has not meaningfully changed across "
                "the last 3 browser actions. Stop exploratory clicking. Use "
                "`inspect` to see visible controls and stable selectors before "
                "trying again."
            )
            if controls_summary:
                note = f"{note}\n\n{controls_summary}"
            output = _append_note(output, note)
            intervention = {
                "kind": "stagnant_state",
                "note": note,
                "stagnant_repeats": self.stagnant_repeats,
                "recommended_action": "inspect",
                "controls_summary": controls_summary,
            }

        return self._finalize_result(
            result,
            metadata=metadata,
            action=action,
            target=target,
            state_hash=state_hash,
            repeated_open=repeated_open,
            output=output,
            stderr=stderr,
            intervention=intervention,
        )

    def _finalize_result(
        self,
        result: ToolExecutionResult,
        *,
        metadata: dict[str, Any],
        action: str,
        target: str,
        state_hash: str,
        repeated_open: bool,
        output: str,
        stderr: str,
        intervention: dict[str, Any] | None,
    ) -> ToolExecutionResult:
        metadata["browser_progress"] = {
            "action": action,
            "target": target,
            "state_hash": state_hash,
            "stagnant_repeats": self.stagnant_repeats,
            "repeat_failures": self.repeat_failures,
            "repeated_open": repeated_open,
            "controls_summary": self.last_controls_summary,
        }
        if intervention is not None:
            metadata["verification_intervention"] = intervention
        else:
            metadata.pop("verification_intervention", None)
        return replace(result, output=output, stderr=stderr, metadata=metadata)


def build_verification_nudge(intervention: dict[str, Any]) -> str:
    """Convert a structured intervention into a continuation reminder."""
    kind = str(intervention.get("kind") or "").strip().lower()
    controls_summary = str(intervention.get("controls_summary") or "").strip()
    if kind == "repeat_failure":
        text = (
            "A browser verification step has failed repeatedly. Do not "
            "retry the same click or selector again. Read the current page "
            "state, call `inspect`, and then choose a more reliable control."
        )
    elif kind == "repeated_open":
        text = (
            "You reopened the same page and got the same state back. Reuse the "
            "existing browser session unless a real reload, code edit, or "
            "storage reset is required."
        )
    elif kind == "stagnant_state":
        text = (
            "Recent browser actions did not materially change the page state. "
            "Stop exploratory clicking. Call `inspect`, use stable selectors, "
            "and make one targeted verification step."
        )
    else:
        text = str(intervention.get("note") or "").strip()
    if controls_summary:
        text = f"{text}\n\n{controls_summary}"
    return text


def _append_note(existing: str, note: str) -> str:
    if not note:
        return existing
    base = existing.rstrip()
    if not base:
        return note
    return f"{base}\n\n{note}"
