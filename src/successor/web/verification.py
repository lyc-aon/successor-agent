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
from ..verification_adoption import looks_stateful_runtime_task


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

_VISUAL_VERIFICATION_KEYWORDS = (
    "visual",
    "layout",
    "spacing",
    "clipping",
    "overflow",
    "hierarchy",
    "contrast",
    "design",
    "polish",
    "animation",
    "look",
    "looks",
    "human",
    "screenshot",
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


def build_browser_verification_guidance(
    *,
    latest_user_text: str,
    active_task_text: str = "",
    vision_available: bool,
    browser_verifier_available: bool,
    browser_verifier_loaded: bool,
) -> str:
    """Return a compact execution block for browser-led verification work."""
    visual = _looks_visual(latest_user_text, active_task_text)
    stateful_runtime = looks_stateful_runtime_task(
        latest_user_text,
        active_task_text,
    )
    lines = ["### Browser verification mode", ""]
    lines.append(
        "- Treat the browser as evidence gathering, not exploration. Open once, inspect if unsure, take the smallest proving action, then read the resulting page state before deciding the next step."
    )
    lines.append(
        "- If the user already gave you a working local URL, or the page is already reachable, treat that runtime as externally managed. Do not kill, restart, or replace its server unless the user asked for that or you have proved the runtime is dead and need your own replacement."
    )
    lines.append(
        "- If you need your own local server and the first-choice port is busy, choose another free port before considering process cleanup. Do not reclaim an occupied port just because it was convenient."
    )
    lines.append(
        "- If this is more than a one-step sanity check and the `verify` tool is available, create or refresh a compact verification item before or during browser work so the claim and evidence stay explicit."
    )
    if browser_verifier_available and not browser_verifier_loaded:
        lines.append(
            "- Before the first browser action, load the `browser-verifier` skill so the verification loop stays selector-driven and bounded."
        )
    lines.append(
        "- For interactive claims, prove the behavior with real browser evidence. Do not mark a claim passed from source inspection, total DOM counts, or intention alone."
    )
    lines.append(
        "- Capture a specific before/after state delta for the feature under test. Verify the exact score, count, label, panel state, URL, toast text, or visible copy that should change after the interaction."
    )
    lines.append(
        "- When checking filters, search, or visibility, verify what is visibly rendered after the interaction. Prefer one decisive interaction plus `extract_text` or an explicit page-state check over broad exploratory clicking."
    )
    lines.append(
        "- After steps that may trigger runtime issues, check `console_errors` before concluding the flow is healthy."
    )
    lines.append(
        "- Run at least one edge or failure-path check that matches the feature under test instead of verifying only the happy path."
    )
    if stateful_runtime:
        lines.append(
            "- This task looks stateful or realtime. If manual browser play is weak, add a tiny deterministic driver, autoplay harness, or player script, then use the browser to observe the driven run instead of relying on hand-play alone."
        )
        lines.append(
            "- Pair that driver with an observable state surface such as a score HUD, runtime log, debug overlay, or state accessor so the main loop can be proved with concrete evidence."
        )
    if visual and vision_available:
        lines.append(
            "- This task is explicitly visual. Capture a `screenshot` and use `vision` before passing layout, spacing, hierarchy, clipping, or design-polish claims."
        )
    elif visual:
        lines.append(
            "- This task is explicitly visual. Capture a `screenshot` and ground your conclusion in the visible page result before passing visual claims."
        )
    elif vision_available:
        lines.append(
            "- If any claim depends on what is visibly on screen rather than DOM text, capture a `screenshot` and use `vision` before passing it."
        )
    lines.append(
        "- Stop once the requested behavior is verified or falsified. Do not tour the whole app after the decisive evidence already exists."
    )
    return "\n".join(lines)


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
    screenshot_streak: int = 0
    action_repeat_counts: dict[tuple[str, str], int] | None = None

    def __post_init__(self) -> None:
        if self.action_repeat_counts is None:
            self.action_repeat_counts = {}

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

        # ─── Action-repeat tracking (catches screenshot loops, etc.) ───
        assert self.action_repeat_counts is not None
        action_key = (action, target)
        self.action_repeat_counts[action_key] = (
            self.action_repeat_counts.get(action_key, 0) + 1
        )
        if action == "screenshot":
            self.screenshot_streak += 1
        else:
            self.screenshot_streak = 0

        # Screenshot streak nudge — 3+ consecutive screenshots without
        # an intervening non-screenshot action
        if self.screenshot_streak >= 3:
            note = (
                f"Progress note: you have taken {self.screenshot_streak} "
                "consecutive screenshots without acting on them. Use "
                "`vision` to analyze a screenshot, or proceed with your "
                "next action."
            )
            output = _append_note(output, note)
            if intervention is None:
                intervention = {
                    "kind": "screenshot_streak",
                    "note": note,
                    "screenshot_streak": self.screenshot_streak,
                    "recommended_action": "vision",
                }

        # Action-repeat nudge — same (action, target) 3+ times
        repeat_count = self.action_repeat_counts[action_key]
        if repeat_count >= 3 and action != "screenshot":
            note = (
                f"Progress note: you have called `{action}` on the same "
                f"target {repeat_count} times. Try a different approach "
                "or verify the previous results before repeating."
            )
            output = _append_note(output, note)
            if intervention is None:
                intervention = {
                    "kind": "action_repeat",
                    "note": note,
                    "action": action,
                    "target": target,
                    "repeat_count": repeat_count,
                }

        # ─── Non-interactive actions (screenshot, extract_text, etc.) ───
        # These don't change page state, so they should NOT reset the
        # stagnant counter. Only update the state hash.
        if action not in {"click", "type", "press", "select", "wait_for"} or not state_hash:
            if state_hash:
                self.last_state_hash = state_hash
            # Intentionally NOT resetting stagnant_repeats — read-only
            # actions shouldn't clear evidence of interactive stagnation
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

        # ─── Interactive actions — stagnant-state detection ───
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


def _looks_visual(*texts: str) -> bool:
    for text in texts:
        lowered = " ".join(str(text or "").lower().split())
        if not lowered:
            continue
        if any(keyword in lowered for keyword in _VISUAL_VERIFICATION_KEYWORDS):
            return True
    return False
