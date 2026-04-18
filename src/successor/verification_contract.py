"""Session-local verification contract for evidence-bearing completion.

The task ledger tracks what work is being done. The verification
contract tracks what must be PROVEN before the work is really done.

This stays session-local, compact, and explicit so the model can keep a
small set of claims plus their intended evidence in view during long
interactive runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


VerificationStatus = Literal["pending", "in_progress", "passed", "failed"]
MAX_ASSERTIONS = 64


class VerificationContractError(ValueError):
    """Raised when a model-emitted verification payload is invalid."""


def _normalize_required_text(value: Any, *, field_name: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise VerificationContractError(
            f"verify.{field_name} must be a non-empty string"
        )
    return text


def _normalize_optional_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_status(value: Any) -> VerificationStatus:
    status = str(value or "").strip().lower()
    if status not in {"pending", "in_progress", "passed", "failed"}:
        raise VerificationContractError(
            "verify.status must be one of: pending, in_progress, passed, failed"
        )
    return status  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class VerificationItem:
    claim: str
    evidence: str
    status: VerificationStatus
    observed: str = ""

    @property
    def done(self) -> bool:
        return self.status in {"passed", "failed"}

    @property
    def in_progress(self) -> bool:
        return self.status == "in_progress"


def parse_verification_items(raw_items: Any) -> tuple[VerificationItem, ...]:
    """Validate and normalize a tool payload into immutable assertions."""
    if raw_items is None:
        raise VerificationContractError("verify.items is required")
    if not isinstance(raw_items, list):
        raise VerificationContractError("verify.items must be an array")
    if len(raw_items) > MAX_ASSERTIONS:
        raise VerificationContractError(
            f"verify.items may contain at most {MAX_ASSERTIONS} entries"
        )

    items: list[VerificationItem] = []
    in_progress_count = 0
    for idx, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise VerificationContractError(f"verify.items[{idx}] must be an object")
        claim = _normalize_required_text(raw_item.get("claim"), field_name="claim")
        evidence = _normalize_required_text(
            raw_item.get("evidence"),
            field_name="evidence",
        )
        status = _normalize_status(raw_item.get("status"))
        observed = _normalize_optional_text(raw_item.get("observed"))
        if status == "in_progress":
            in_progress_count += 1
        items.append(
            VerificationItem(
                claim=claim,
                evidence=evidence,
                status=status,
                observed=observed,
            )
        )

    if in_progress_count > 1:
        raise VerificationContractError(
            "verify.items may contain at most one in_progress item"
        )
    return tuple(items)


@dataclass(slots=True)
class VerificationLedger:
    items: tuple[VerificationItem, ...] = field(default_factory=tuple)

    def replace(self, items: tuple[VerificationItem, ...]) -> None:
        self.items = tuple(items)

    def clear(self) -> None:
        self.items = ()

    def has_items(self) -> bool:
        return bool(self.items)

    def has_in_progress(self) -> bool:
        return any(item.in_progress for item in self.items)

    def in_progress_item(self) -> VerificationItem | None:
        for item in self.items:
            if item.in_progress:
                return item
        return None

    def pending_count(self) -> int:
        return sum(1 for item in self.items if item.status == "pending")

    def passed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "passed")

    def failed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    def open_count(self) -> int:
        return sum(1 for item in self.items if not item.done)

    def is_all_passed(self) -> bool:
        """True iff the contract has at least one item AND every item is passed.

        An empty ledger is not "all passed" — there's nothing to pass. An
        all-passed contract is the signal that the work is provably done.
        """
        if not self.items:
            return False
        return all(item.status == "passed" for item in self.items)


def verification_items_to_payload(
    items: tuple[VerificationItem, ...],
) -> list[dict[str, str]]:
    return [
        {
            "claim": item.claim,
            "evidence": item.evidence,
            "status": item.status,
            "observed": item.observed,
        }
        for item in items
    ]


def build_verification_card_output(ledger: VerificationLedger) -> str:
    if not ledger.items:
        return "Cleared the session verification contract."
    lines = ["Updated the session verification contract."]
    for item in ledger.items:
        label = {
            "pending": "pending",
            "in_progress": "in progress",
            "passed": "passed",
            "failed": "failed",
        }[item.status]
        lines.append(f"- [{label}] {item.claim}")
        lines.append(f"  evidence: {item.evidence}")
        if item.observed:
            lines.append(f"  observed: {item.observed}")
    return "\n".join(lines)


def build_verification_tool_result(ledger: VerificationLedger) -> str:
    lines = [
        "<verification-contract>",
        f"<assertion-count>{len(ledger.items)}</assertion-count>",
    ]
    active = ledger.in_progress_item()
    if active is not None:
        lines.append(f"<active-claim>{active.claim}</active-claim>")
        lines.append(f"<active-evidence>{active.evidence}</active-evidence>")
    for item in ledger.items:
        lines.extend(
            [
                "<assertion>",
                f"<status>{item.status}</status>",
                f"<claim>{item.claim}</claim>",
                f"<evidence>{item.evidence}</evidence>",
                f"<observed>{item.observed}</observed>",
                "</assertion>",
            ]
        )
    lines.append("</verification-contract>")
    return "\n".join(lines)


def build_verification_prompt_section(ledger: VerificationLedger) -> str:
    lines = ["## Current Verification Contract", ""]
    if not ledger.items:
        lines.append("No current verification contract.")
        return "\n".join(lines)
    for item in ledger.items:
        lines.append(f"- [{item.status}] {item.claim}")
        lines.append(f"  evidence: {item.evidence}")
        if item.observed:
            lines.append(f"  observed: {item.observed}")
    return "\n".join(lines)


def build_verification_execution_primer(
    *,
    subagent_available: bool = False,
    stateful_runtime: bool = False,
) -> str:
    lines = ["### Evidence-bearing verification", ""]
    lines.extend([
        "- For interactive, stateful, or browser-facing work, create or update a compact verification contract early.",
        "- Each item should name the claim to prove and the concrete evidence that will prove it.",
        "- Prefer executable evidence: browser interactions, screenshots plus vision, console/runtime checks, command output, or a tiny verifier/player script.",
        "- For interactive claims, capture a specific before/after state delta. Name the exact score, count, label, panel state, URL, or visible text that should change, then prove that it changed.",
        "- If direct manual checking is weak, repetitive, or impossible, add a temporary structured debug surface or debug logs that expose the exact state transitions you need to prove.",
        "- Prefer debug logs that answer concrete questions like input received, state changed, animation advanced, collision fired, or persistence wrote.",
        "- For non-trivial work, run at least one adversarial or failure-path probe instead of verifying only the happy path.",
        "- Do not mark an item `passed` from source inspection alone. Update it only after the real evidence exists.",
        "- Mark an item `failed` when the observed evidence contradicts the claim, and record the concise observed outcome.",
        "- Skip the verification contract only for single trivial tasks or purely conversational replies.",
    ])
    if stateful_runtime:
        lines.extend([
            "- This task looks stateful or realtime. Build the evaluator as part of the work instead of treating verification as an afterthought.",
            "- Prefer a tiny deterministic driver, autoplay harness, or player script over casual manual play when the runtime is fast, noisy, or timing-sensitive.",
            "- Name that driver explicitly in the verification contract, and pair it with an observable debug surface such as a HUD value, runtime log, or state accessor.",
        ])
    # NOTE: the "launch a fresh read-only subagent" nudge was removed
    # 2026-04-17. It was re-injected every turn alongside the rest of this
    # guidance and actively kept capable models (GLM 5.1, Claude) in
    # verification loops — they read "consider more verification" and
    # kept calling browser tools instead of self-terminating. The
    # contract's existence is sufficient signal. If subagent-based
    # verification is valuable for a specific workflow, surface it via
    # a skill the model loads on demand, not per-turn injection.
    _ = subagent_available  # intentionally unused; kept for API stability
    return "\n".join(lines)


def build_verification_execution_guidance(
    ledger: VerificationLedger,
    *,
    subagent_available: bool = False,
    stateful_runtime: bool = False,
) -> str:
    if not ledger.items:
        return build_verification_execution_primer(
            subagent_available=subagent_available,
            stateful_runtime=stateful_runtime,
        )
    lines = ["### Evidence-bearing verification", ""]
    active = ledger.in_progress_item()
    if active is not None:
        lines.append(
            f"- A verification item is already `in_progress`: `{active.claim}`. Keep gathering evidence until it can honestly become `passed` or `failed`."
        )
    else:
        lines.append(
            "- A verification contract already exists. Update it as new evidence arrives so it stays authoritative."
        )
    lines.extend([
        "- Prefer executable evidence: browser interactions, screenshots plus vision, console/runtime checks, command output, or a tiny verifier/player script.",
        "- For interactive claims, capture a specific before/after state delta. Name the exact score, count, label, panel state, URL, or visible text that should change, then prove that it changed.",
        "- If direct manual checking is weak, repetitive, or impossible, add a temporary structured debug surface or debug logs that expose the exact state transitions you need to prove.",
        "- Prefer debug logs that answer concrete questions like input received, state changed, animation advanced, collision fired, or persistence wrote.",
        "- For non-trivial work, run at least one adversarial or failure-path probe instead of verifying only the happy path.",
        "- Do not mark an item `passed` from source inspection alone. Update it only after the real evidence exists.",
        "- Mark an item `failed` when the observed evidence contradicts the claim, and record the concise observed outcome.",
        "- Skip the verification contract only for single trivial tasks or purely conversational replies.",
    ])
    if stateful_runtime:
        lines.extend([
            "- This task looks stateful or realtime. Build the evaluator as part of the work instead of treating verification as an afterthought.",
            "- Prefer a tiny deterministic driver, autoplay harness, or player script over casual manual play when the runtime is fast, noisy, or timing-sensitive.",
            "- Name that driver explicitly in the verification contract, and pair it with an observable debug surface such as a HUD value, runtime log, or state accessor.",
        ])
    # NOTE: the "launch a fresh read-only subagent" nudge was removed
    # 2026-04-17. It was re-injected every turn alongside the rest of this
    # guidance and actively kept capable models (GLM 5.1, Claude) in
    # verification loops — they read "consider more verification" and
    # kept calling browser tools instead of self-terminating. The
    # contract's existence is sufficient signal. If subagent-based
    # verification is valuable for a specific workflow, surface it via
    # a skill the model loads on demand, not per-turn injection.
    _ = subagent_available  # intentionally unused; kept for API stability
    return "\n".join(lines)


def build_verification_continue_nudge(ledger: VerificationLedger) -> str:
    active = ledger.in_progress_item()
    if active is None:
        return ""
    return (
        "A verification item is still marked `in_progress`: "
        f"`{active.claim}`. If the work is still being verified, keep gathering "
        "the named evidence now instead of handing control back. If the check is "
        "actually complete or disproven, call the `verify` tool first so the "
        "contract reflects that before you stop."
    )


def build_verification_settled_nudge(ledger: VerificationLedger) -> str:
    """Short terminal nudge when the contract transitions to all-passed.

    Injected ONCE via a one-shot at the moment the ledger settles so a
    capable model (Claude, GLM 5.1, etc.) gets a clear "you're done" signal
    and doesn't keep tool-calling past the point of sufficient evidence.
    """
    if not ledger.is_all_passed():
        return ""
    passed = len(ledger.items)
    plural = "item" if passed == 1 else "items"
    return (
        f"All {passed} verification {plural} are passed. The contract is "
        "fully satisfied. Reply with plain text to return control to the "
        "user. Do not run additional browser, screenshot, or vision calls "
        "unless the user explicitly asks for more."
    )


def build_assertions_artifact(ledger: VerificationLedger) -> dict[str, object]:
    active = ledger.in_progress_item()
    status = "empty"
    if ledger.items:
        if ledger.failed_count() > 0:
            status = "failed"
        elif ledger.has_in_progress():
            status = "running"
        elif ledger.pending_count() > 0:
            status = "pending"
        elif ledger.passed_count() == len(ledger.items):
            status = "passed"
        else:
            status = "mixed"
    return {
        "status": status,
        "total": len(ledger.items),
        "pending": ledger.pending_count(),
        "in_progress": 1 if ledger.has_in_progress() else 0,
        "passed": ledger.passed_count(),
        "failed": ledger.failed_count(),
        "active_claim": active.claim if active else "",
        "items": verification_items_to_payload(ledger.items),
    }
