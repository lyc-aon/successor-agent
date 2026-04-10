"""Unit coverage for long-horizon task/runbook adoption nudges."""

from __future__ import annotations

from types import SimpleNamespace

from successor.runbook import SessionRunbook
from successor.task_adoption import (
    looks_long_horizon_task,
    maybe_build_task_adoption_nudge,
)
from successor.tasks import SessionTaskLedger, parse_task_items


def _tool_message(tool_name: str, *, risk: str = "safe") -> SimpleNamespace:
    return SimpleNamespace(
        role="tool",
        raw_text="",
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


def test_long_horizon_detection_catches_supervised_build_verify_work() -> None:
    assert looks_long_horizon_task(
        "Build a browser game, iterate on it across several turns, record the session, and verify the runtime and visuals thoroughly.",
    )
    assert not looks_long_horizon_task("Answer what pwd does in bash.")


def test_preflight_task_adoption_nudge_requests_ledger_before_big_work() -> None:
    decision = maybe_build_task_adoption_nudge(
        latest_user_text=(
            "Build a browser typing game, verify runtime behavior honestly, "
            "and keep iterating over several turns with recording."
        ),
        active_task_text="",
        ledger=SessionTaskLedger(),
        runbook=SessionRunbook(),
        messages=[_user_message("Build the game and verify it.")],
    )

    assert decision.should_nudge is True
    assert decision.kind == "missing_ledger_preflight"
    assert "call `task` with 3-6 coarse steps" in decision.text
    assert "set up `verify` early" in decision.text


def test_posthoc_task_adoption_nudge_escalates_after_substantive_work() -> None:
    decision = maybe_build_task_adoption_nudge(
        latest_user_text=(
            "Build a browser bullet-hell game, verify the runtime honestly, "
            "and iterate until it works."
        ),
        active_task_text="canvas game loop browser verification",
        ledger=SessionTaskLedger(),
        runbook=SessionRunbook(),
        messages=[
            _user_message("Build a browser bullet-hell game and verify it."),
            _tool_message("write_file", risk="mutating"),
            _tool_message("browser"),
        ],
    )

    assert decision.should_nudge is True
    assert decision.kind == "missing_ledger_after_work"
    assert "already underway without a session task ledger" in decision.text
    assert "initialize `runbook` early" in decision.text


def test_missing_runbook_nudge_fires_after_multiple_iterative_actions() -> None:
    ledger = SessionTaskLedger()
    ledger.replace(parse_task_items([
        {
            "content": "Build the game shell",
            "active_form": "building the game shell",
            "status": "in_progress",
        }
    ]))

    decision = maybe_build_task_adoption_nudge(
        latest_user_text="Build a browser game, compare runtime behavior, and iterate.",
        active_task_text="game loop compare runtime iterate",
        ledger=ledger,
        runbook=SessionRunbook(),
        messages=[
            _user_message("Build a browser game and iterate."),
            _tool_message("write_file", risk="mutating"),
            _tool_message("browser"),
            _tool_message("verify"),
        ],
    )

    assert decision.should_nudge is True
    assert decision.kind == "missing_runbook"
    assert "lacks a `runbook`" in decision.text
