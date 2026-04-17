"""Unit coverage for the session-local experimental runbook."""

from __future__ import annotations

import pytest

from successor.runbook import (
    RunbookError,
    SessionRunbook,
    build_runbook_artifact,
    build_runbook_card_output,
    build_runbook_execution_guidance,
    build_runbook_prompt_section,
    build_runbook_tool_result,
    parse_experiment_attempt,
    parse_runbook_state,
)


def test_parse_runbook_state_normalizes_core_fields() -> None:
    state = parse_runbook_state(
        {
            "objective": " Ship a stable typing loop ",
            "success_definition": " scripted playthrough works end to end ",
            "scope": [" src/game ", " src/ui "],
            "baseline_status": "captured",
            "baseline_summary": "Current build opens but stalls after wave one",
            "active_hypothesis": "Focus is being lost after transitions",
            "evaluator": [
                {
                    "id": "build",
                    "kind": "command",
                    "spec": "npm run build",
                    "pass_condition": "exit 0",
                }
            ],
            "status": "running",
        }
    )

    assert state is not None
    assert state.objective == "Ship a stable typing loop"
    assert state.scope == ("src/game", "src/ui")
    assert state.evaluator[0].step_id == "build"
    assert state.status == "running"


def test_parse_runbook_state_supports_clear() -> None:
    assert parse_runbook_state({"clear": True}) is None


def test_parse_experiment_attempt_rejects_bad_decision() -> None:
    with pytest.raises(RunbookError, match="attempt.decision"):
        parse_experiment_attempt(
            {
                "hypothesis": "bad",
                "summary": "bad",
                "decision": "ship-it",
            },
            next_attempt_id=1,
        )


def test_runbook_helpers_render_contract_and_attempt() -> None:
    state = parse_runbook_state(
        {
            "objective": "Ship a stable typing loop",
            "success_definition": "Scripted player and browser verification both pass",
            "baseline_status": "missing",
            "active_hypothesis": "Input focus is lost after transitions",
            "evaluator": [
                {
                    "id": "build",
                    "kind": "command",
                    "spec": "npm run build",
                    "pass_condition": "exit 0",
                }
            ],
            "status": "running",
        }
    )
    assert state is not None
    attempt = parse_experiment_attempt(
        {
            "hypothesis": "Locking focus after wave transitions fixes the stall",
            "summary": "Build passed and player advanced to wave three",
            "decision": "kept",
            "files_touched": ["src/game/input.ts"],
            "evaluator_summary": "build and player script both passed",
            "verification_summary": "browser HUD updated correctly",
        },
        next_attempt_id=2,
    )
    assert attempt is not None

    prompt_section = build_runbook_prompt_section(SessionRunbook(state=state))
    assert "## Current Runbook" in prompt_section
    assert "Ship a stable typing loop" in prompt_section

    guidance = build_runbook_execution_guidance(SessionRunbook(state=state))
    assert "Experimental run discipline" in guidance
    assert "baseline is `missing`" in guidance

    card_output = build_runbook_card_output(state, attempt=attempt)
    assert "Updated the session runbook." in card_output
    assert "recorded attempt 2 [kept]" in card_output

    tool_result = build_runbook_tool_result(state, attempt=attempt)
    assert "<runbook>" in tool_result
    assert "<latest-attempt>" in tool_result

    artifact = build_runbook_artifact(state, attempt_count=2, last_attempt=attempt)
    assert artifact["configured"] is True
    assert artifact["attempt_count"] == 2


# ─── Partial update tests ───


def test_partial_update_status_only() -> None:
    """Partial update with just status when existing state exists."""
    existing = parse_runbook_state(
        {
            "objective": "Build the game",
            "success_definition": "It works end to end",
        }
    )
    assert existing is not None

    updated = parse_runbook_state(
        {"status": "running"},
        existing=existing,
    )
    assert updated is not None
    assert updated.objective == "Build the game"
    assert updated.success_definition == "It works end to end"
    assert updated.status == "running"


def test_partial_update_hypothesis() -> None:
    """Partial update changes only the provided fields."""
    existing = parse_runbook_state(
        {
            "objective": "Ship roguelike",
            "success_definition": "Win condition reachable",
            "active_hypothesis": "old idea",
            "status": "planning",
        }
    )
    assert existing is not None

    updated = parse_runbook_state(
        {"active_hypothesis": "new idea", "status": "running"},
        existing=existing,
    )
    assert updated is not None
    assert updated.objective == "Ship roguelike"
    assert updated.active_hypothesis == "new idea"
    assert updated.status == "running"
    # Unchanged fields inherited
    assert updated.success_definition == "Win condition reachable"


def test_partial_update_scope() -> None:
    """Partial update can replace scope without touching objective."""
    existing = parse_runbook_state(
        {
            "objective": "Build dungeon generator",
            "success_definition": "Rooms and corridors connect",
            "scope": ["src/gen.py"],
        }
    )
    assert existing is not None

    updated = parse_runbook_state(
        {"scope": ["src/gen.py", "src/tiles.py"]},
        existing=existing,
    )
    assert updated is not None
    assert updated.objective == "Build dungeon generator"
    assert updated.scope == ("src/gen.py", "src/tiles.py")


def test_partial_update_clears_runbook() -> None:
    """Clear works even with existing state provided."""
    existing = parse_runbook_state(
        {
            "objective": "Build the game",
            "success_definition": "It works",
        }
    )
    assert existing is not None

    result = parse_runbook_state({"clear": True}, existing=existing)
    assert result is None


def test_partial_update_without_existing_requires_objective() -> None:
    """Missing objective with no existing state gives helpful error."""
    with pytest.raises(
        RunbookError,
        match="runbook.objective is required when creating a new runbook",
    ):
        parse_runbook_state({"status": "running"})


def test_full_create_ignores_existing() -> None:
    """Full create (with objective) replaces everything, ignores existing."""
    existing = parse_runbook_state(
        {
            "objective": "Old objective",
            "success_definition": "Old success",
            "active_hypothesis": "old hypothesis",
            "scope": ["old.py"],
        }
    )
    assert existing is not None

    updated = parse_runbook_state(
        {
            "objective": "New objective",
            "success_definition": "New success",
        },
        existing=existing,
    )
    assert updated is not None
    assert updated.objective == "New objective"
    assert updated.success_definition == "New success"
    # Fields not provided in full create reset to defaults
    assert updated.active_hypothesis == ""
    assert updated.scope == ()
