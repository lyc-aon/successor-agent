"""Unit coverage for stateful-runtime verification adoption nudges."""

from __future__ import annotations

from types import SimpleNamespace

from successor.verification_adoption import (
    looks_stateful_runtime_task,
    maybe_build_verification_adoption_nudge,
    summarize_verification_coverage,
)
from successor.verification_contract import VerificationLedger, parse_verification_items


def _tool_message(tool_name: str, *, risk: str = "safe") -> SimpleNamespace:
    return SimpleNamespace(
        role="tool",
        synthetic=False,
        is_summary=False,
        subagent_card=None,
        tool_card=SimpleNamespace(tool_name=tool_name, risk=risk),
    )


def _user_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        role="user",
        raw_text=text,
        synthetic=False,
        is_summary=False,
        subagent_card=None,
        tool_card=None,
    )


def test_stateful_runtime_keyword_detection_is_broad_but_bounded() -> None:
    assert looks_stateful_runtime_task("Build a browser snake game with arrow keys.")
    assert looks_stateful_runtime_task("Add a canvas animation and physics loop.")
    assert not looks_stateful_runtime_task("Refactor the settings form labels.")


def test_verification_coverage_detects_driver_and_observability() -> None:
    ledger = VerificationLedger(
        items=parse_verification_items([
            {
                "claim": "Snake moves and scores correctly",
                "evidence": "deterministic driver script runs autoplay against the seeded board",
                "status": "in_progress",
                "observed": "runtime log and score HUD both advance on food pickup",
            }
        ])
    )

    coverage = summarize_verification_coverage(ledger)

    assert coverage.has_driver is True
    assert coverage.has_observability is True
    assert coverage.complete is True


def test_stateful_runtime_nudge_requires_substantive_work() -> None:
    decision = maybe_build_verification_adoption_nudge(
        latest_user_text="Build a typing game that teaches linux commands.",
        active_task_text="",
        ledger=VerificationLedger(),
        messages=[_user_message("Build a typing game that teaches linux commands.")],
    )

    assert decision.should_nudge is False
    assert decision.stateful_runtime is True


def test_stateful_runtime_nudge_requests_driver_and_observability() -> None:
    decision = maybe_build_verification_adoption_nudge(
        latest_user_text="Build a snake game and verify the runtime honestly.",
        active_task_text="game loop score collision browser verification",
        ledger=VerificationLedger(),
        messages=[
            _user_message("Build a snake game and verify the runtime honestly."),
            _tool_message("write_file", risk="mutating"),
            _tool_message("browser"),
        ],
    )

    assert decision.should_nudge is True
    assert decision.kind == "missing_contract"
    assert "deterministic driver" in decision.text
    assert "HUD, or state log" in decision.text


def test_complete_stateful_contract_suppresses_adoption_nudge() -> None:
    ledger = VerificationLedger(
        items=parse_verification_items([
            {
                "claim": "The game loop remains stable during autoplay",
                "evidence": "deterministic driver script replays inputs across a seeded board",
                "status": "in_progress",
                "observed": "runtime log and debug HUD stay in sync for 200 ticks",
            }
        ])
    )

    decision = maybe_build_verification_adoption_nudge(
        latest_user_text="Build a browser game and verify it.",
        active_task_text="canvas score debug HUD",
        ledger=ledger,
        messages=[
            _user_message("Build a browser game and verify it."),
            _tool_message("write_file", risk="mutating"),
            _tool_message("verify"),
            _tool_message("browser"),
        ],
    )

    assert decision.should_nudge is False
    assert decision.coverage.complete is True
