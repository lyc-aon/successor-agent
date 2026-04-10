"""Helpers for nudging better verification on stateful/realtime work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .verification_contract import VerificationLedger

_STATEFUL_RUNTIME_KEYWORDS = (
    "game",
    "snake",
    "tetris",
    "pong",
    "breakout",
    "typing game",
    "canvas",
    "webgl",
    "animation",
    "animated",
    "realtime",
    "real-time",
    "real time",
    "fast paced",
    "fast-paced",
    "requestanimationframe",
    "game loop",
    "physics",
    "collision",
    "score",
    "lives",
    "wave",
    "spawn",
    "enemy",
    "timer",
    "tick",
    "frame",
    "fps",
    "keyboard",
    "arrow keys",
    "wasd",
    "drag",
    "drop",
    "simulation",
    "simulator",
    "autoplay",
    "autoplayer",
    "bot",
)

_DRIVER_EVIDENCE_KEYWORDS = (
    "scripted player",
    "player script",
    "play script",
    "driver script",
    "deterministic driver",
    "autoplay",
    "autoplayer",
    "bot",
    "harness",
    "seeded runtime",
    "seeded board",
    "simulate input",
    "simulated input",
    "replay driver",
)

_OBSERVABILITY_EVIDENCE_KEYWORDS = (
    "debug",
    "window.__",
    "state log",
    "runtime log",
    "score hud",
    "hud",
    "tick log",
    "collision log",
    "state accessor",
    "telemetry",
    "trace",
    "console log",
)


@dataclass(frozen=True, slots=True)
class SubmissionActivity:
    tool_cards: int = 0
    mutation_actions: int = 0
    browser_actions: int = 0
    verify_updates: int = 0

    @property
    def substantive_actions(self) -> int:
        return self.mutation_actions + self.browser_actions


@dataclass(frozen=True, slots=True)
class VerificationCoverage:
    has_driver: bool = False
    has_observability: bool = False

    @property
    def complete(self) -> bool:
        return self.has_driver and self.has_observability


@dataclass(frozen=True, slots=True)
class VerificationAdoptionDecision:
    should_nudge: bool
    kind: str = ""
    stateful_runtime: bool = False
    activity: SubmissionActivity = SubmissionActivity()
    coverage: VerificationCoverage = VerificationCoverage()
    text: str = ""


def looks_stateful_runtime_task(*texts: str) -> bool:
    """Return True when the task looks stateful, interactive, or realtime."""
    for text in texts:
        lowered = " ".join(str(text or "").lower().split())
        if not lowered:
            continue
        if any(keyword in lowered for keyword in _STATEFUL_RUNTIME_KEYWORDS):
            return True
    return False


def summarize_submission_activity(messages: list[Any]) -> SubmissionActivity:
    """Summarize tool usage since the latest real user message."""
    tool_cards = 0
    mutation_actions = 0
    browser_actions = 0
    verify_updates = 0

    for msg in reversed(messages):
        role = getattr(msg, "role", "")
        synthetic = bool(getattr(msg, "synthetic", False))
        tool_card = getattr(msg, "tool_card", None)
        subagent_card = getattr(msg, "subagent_card", None)
        is_summary = bool(getattr(msg, "is_summary", False))
        if role == "user" and not synthetic and tool_card is None and subagent_card is None and not is_summary:
            break
        if tool_card is None:
            continue
        tool_cards += 1
        tool_name = str(getattr(tool_card, "tool_name", "") or "")
        if tool_name == "browser":
            browser_actions += 1
        elif tool_name == "verify":
            verify_updates += 1
        elif tool_name in {"write_file", "edit_file"}:
            mutation_actions += 1
        elif tool_name == "bash" and str(getattr(tool_card, "risk", "safe")) in {
            "mutating",
            "dangerous",
        }:
            mutation_actions += 1

    return SubmissionActivity(
        tool_cards=tool_cards,
        mutation_actions=mutation_actions,
        browser_actions=browser_actions,
        verify_updates=verify_updates,
    )


def summarize_verification_coverage(ledger: VerificationLedger) -> VerificationCoverage:
    """Return whether the current contract names a driver and debug surface."""
    has_driver = False
    has_observability = False
    for item in ledger.items:
        combined = " ".join(
            part for part in (item.claim, item.evidence, item.observed) if part
        ).lower()
        if any(keyword in combined for keyword in _DRIVER_EVIDENCE_KEYWORDS):
            has_driver = True
        if any(keyword in combined for keyword in _OBSERVABILITY_EVIDENCE_KEYWORDS):
            has_observability = True
    return VerificationCoverage(
        has_driver=has_driver,
        has_observability=has_observability,
    )


def maybe_build_verification_adoption_nudge(
    *,
    latest_user_text: str,
    active_task_text: str,
    ledger: VerificationLedger,
    messages: list[Any],
) -> VerificationAdoptionDecision:
    """Return a bounded reminder when realtime work lacks a good verifier."""
    stateful_runtime = looks_stateful_runtime_task(latest_user_text, active_task_text)
    activity = summarize_submission_activity(messages)
    coverage = summarize_verification_coverage(ledger)

    if not stateful_runtime:
        return VerificationAdoptionDecision(
            should_nudge=False,
            stateful_runtime=False,
            activity=activity,
            coverage=coverage,
        )
    if activity.substantive_actions < 1:
        return VerificationAdoptionDecision(
            should_nudge=False,
            stateful_runtime=True,
            activity=activity,
            coverage=coverage,
        )
    if ledger.has_items() and coverage.complete:
        return VerificationAdoptionDecision(
            should_nudge=False,
            stateful_runtime=True,
            activity=activity,
            coverage=coverage,
        )

    if not ledger.has_items():
        text = (
            "This task looks stateful or realtime and substantive work is already "
            "underway. Casual manual inspection is weak proof here. Call the "
            "`verify` tool now with 1-3 compact items. Include at least one item "
            "whose evidence names the deterministic driver, autoplay harness, or "
            "player script you will run, and at least one item whose evidence "
            "names the debug surface, HUD, or state log you will read."
        )
        return VerificationAdoptionDecision(
            should_nudge=True,
            kind="missing_contract",
            stateful_runtime=True,
            activity=activity,
            coverage=coverage,
            text=text,
        )

    missing_driver = not coverage.has_driver
    missing_observability = not coverage.has_observability
    if missing_driver and missing_observability:
        text = (
            "This realtime verification contract is still too loose. Refresh "
            "`verify` so one item explicitly names the deterministic driver, "
            "autoplay harness, or player script, and another item explicitly "
            "names the HUD, debug surface, or runtime state log you will read."
        )
        kind = "missing_driver_and_observability"
    elif missing_driver:
        text = (
            "This realtime task already has a verification contract, but it "
            "does not name a deterministic driver, autoplay harness, or player "
            "script yet. Refresh `verify` so that proof path is explicit."
        )
        kind = "missing_driver"
    else:
        text = (
            "This realtime task already has a verification contract, but it "
            "does not name the observable HUD, debug surface, or runtime state "
            "log you will read. Refresh `verify` so the proof path is legible."
        )
        kind = "missing_observability"
    return VerificationAdoptionDecision(
        should_nudge=True,
        kind=kind,
        stateful_runtime=True,
        activity=activity,
        coverage=coverage,
        text=text,
    )
