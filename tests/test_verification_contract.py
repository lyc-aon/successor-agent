"""Unit coverage for the session-local verification contract."""

from __future__ import annotations

import pytest

from successor.verification_contract import (
    VerificationContractError,
    VerificationLedger,
    build_assertions_artifact,
    build_verification_card_output,
    build_verification_execution_guidance,
    build_verification_prompt_section,
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

    guidance = build_verification_execution_guidance(ledger)
    assert "Evidence-bearing verification" in guidance
    assert "debug logs" in guidance
