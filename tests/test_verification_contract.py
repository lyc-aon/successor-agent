"""Unit coverage for the session-local verification contract."""

from __future__ import annotations

import pytest

from successor.verification_contract import (
    VerificationContractError,
    VerificationLedger,
    build_assertions_artifact,
    build_verification_card_output,
    build_verification_continue_nudge,
    build_verification_execution_guidance,
    build_verification_prompt_section,
    build_verification_settled_nudge,
    build_verification_tool_result,
    parse_verification_items,
)


def test_parse_verification_items_normalizes_and_tracks_active_item() -> None:
    items = parse_verification_items([
        {
            "claim": "  CTA launches the modal  ",
            "evidence": " browser click on the hero CTA ",
            "status": "in_progress",
        },
        {
            "claim": "No console errors",
            "evidence": "console_errors output",
            "status": "pending",
            "observed": " ",
        },
    ])

    assert len(items) == 2
    assert items[0].claim == "CTA launches the modal"
    assert items[0].evidence == "browser click on the hero CTA"
    assert items[1].observed == ""

    ledger = VerificationLedger(items=items)
    assert ledger.has_items() is True
    assert ledger.has_in_progress() is True
    assert ledger.in_progress_item() == items[0]


def test_parse_verification_items_rejects_multiple_in_progress_items() -> None:
    with pytest.raises(VerificationContractError, match="at most one in_progress"):
        parse_verification_items([
            {
                "claim": "First claim",
                "evidence": "first evidence",
                "status": "in_progress",
            },
            {
                "claim": "Second claim",
                "evidence": "second evidence",
                "status": "in_progress",
            },
        ])


def test_verification_helpers_render_contract_and_summary() -> None:
    ledger = VerificationLedger(
        items=parse_verification_items([
            {
                "claim": "Hero CTA opens settings",
                "evidence": "browser click opens settings panel",
                "status": "passed",
                "observed": "settings drawer opened",
            },
            {
                "claim": "No runtime errors",
                "evidence": "browser console stays clean during playthrough",
                "status": "in_progress",
            },
        ])
    )

    artifact = build_assertions_artifact(ledger)
    assert artifact["status"] == "running"
    assert artifact["passed"] == 1
    assert artifact["active_claim"] == "No runtime errors"

    card_output = build_verification_card_output(ledger)
    assert "Updated the session verification contract." in card_output
    assert "browser click opens settings panel" in card_output
    assert "settings drawer opened" in card_output

    prompt_section = build_verification_prompt_section(ledger)
    assert "## Current Verification Contract" in prompt_section
    assert "Hero CTA opens settings" in prompt_section

    tool_result = build_verification_tool_result(ledger)
    assert "<verification-contract>" in tool_result
    assert "<active-claim>No runtime errors</active-claim>" in tool_result

    guidance = build_verification_execution_guidance(
        ledger,
        subagent_available=True,
        stateful_runtime=True,
    )
    assert "Evidence-bearing verification" in guidance
    assert "debug logs" in guidance
    assert "before/after state delta" in guidance
    assert "adversarial or failure-path probe" in guidance
    # The 'role="verification"' subagent nudge used to be asserted here;
    # it was removed from the guidance 2026-04-17 to stop re-injecting
    # "launch a verifier subagent" on every turn in long verification
    # loops. Stateful-runtime guidance is still present below.
    assert "deterministic driver" in guidance
    assert "HUD value" in guidance

    nudge = build_verification_continue_nudge(ledger)
    assert "still marked `in_progress`" in nudge
    assert "No runtime errors" in nudge


def test_verification_continue_nudge_is_empty_without_active_item() -> None:
    empty = VerificationLedger()
    assert build_verification_continue_nudge(empty) == ""

    passed_only = VerificationLedger(
        items=parse_verification_items([
            {
                "claim": "It works",
                "evidence": "runtime proof",
                "status": "passed",
            }
        ])
    )
    assert build_verification_continue_nudge(passed_only) == ""


def test_ledger_is_all_passed_predicate() -> None:
    """Empty ledger is NOT all_passed (nothing to pass); all-passed
    items return True; mixed statuses return False."""
    assert VerificationLedger().is_all_passed() is False

    mixed = VerificationLedger(items=parse_verification_items([
        {"claim": "one", "evidence": "e1", "status": "passed"},
        {"claim": "two", "evidence": "e2", "status": "pending"},
    ]))
    assert mixed.is_all_passed() is False

    with_failed = VerificationLedger(items=parse_verification_items([
        {"claim": "one", "evidence": "e1", "status": "passed"},
        {"claim": "two", "evidence": "e2", "status": "failed"},
    ]))
    assert with_failed.is_all_passed() is False

    all_passed = VerificationLedger(items=parse_verification_items([
        {"claim": "one", "evidence": "e1", "status": "passed"},
        {"claim": "two", "evidence": "e2", "status": "passed"},
    ]))
    assert all_passed.is_all_passed() is True


def test_settled_nudge_fires_only_when_all_passed() -> None:
    """The contract-settled nudge is the harness's 'you're done' stop
    signal for capable models. It must only fire when every item is
    passed — not when pending/in-progress/failed items remain."""
    # Empty ledger → no nudge (nothing to be done about)
    assert build_verification_settled_nudge(VerificationLedger()) == ""

    # Has an in-progress item → still working, no nudge
    working = VerificationLedger(items=parse_verification_items([
        {"claim": "one", "evidence": "e1", "status": "in_progress"},
    ]))
    assert build_verification_settled_nudge(working) == ""

    # Has a failed item → stop signal is wrong, don't fire
    failed = VerificationLedger(items=parse_verification_items([
        {"claim": "one", "evidence": "e1", "status": "passed"},
        {"claim": "two", "evidence": "e2", "status": "failed"},
    ]))
    assert build_verification_settled_nudge(failed) == ""

    # All passed → fire the stop signal with a clear message
    all_passed = VerificationLedger(items=parse_verification_items([
        {"claim": "one", "evidence": "e1", "status": "passed"},
        {"claim": "two", "evidence": "e2", "status": "passed"},
    ]))
    nudge = build_verification_settled_nudge(all_passed)
    assert nudge != ""
    # The nudge must include the "reply with plain text" directive or
    # its intent won't land.
    assert "plain text" in nudge
    # And it must specifically tell the model NOT to keep tool-calling.
    assert "Do not run additional" in nudge
