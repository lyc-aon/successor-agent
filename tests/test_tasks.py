"""Unit tests for the session-local task ledger."""

from __future__ import annotations

import pytest

from successor.tasks import (
    SessionTaskLedger,
    TaskLedgerError,
    build_task_card_output,
    build_task_continue_nudge,
    build_task_prompt_section,
    build_task_tool_result,
    parse_task_items,
    task_items_to_payload,
)


def test_parse_task_items_normalizes_and_defaults_active_form() -> None:
    items = parse_task_items([
        {
            "content": "  Build   issue desk  ",
            "status": "in_progress",
        },
        {
            "content": "Run browser verification",
            "active_form": "running browser verification",
            "status": "pending",
        },
    ])

    assert len(items) == 2
    assert items[0].content == "Build issue desk"
    assert items[0].active_form == "Build issue desk"
    assert items[0].status == "in_progress"
    assert items[1].active_form == "running browser verification"


def test_parse_task_items_rejects_multiple_in_progress_tasks() -> None:
    with pytest.raises(TaskLedgerError, match="at most one in_progress"):
        parse_task_items([
            {"content": "One", "status": "in_progress"},
            {"content": "Two", "status": "in_progress"},
        ])


def test_task_ledger_helpers_and_rendering() -> None:
    ledger = SessionTaskLedger()
    ledger.replace(parse_task_items([
        {
            "content": "Inspect browser loop",
            "active_form": "inspecting browser loop",
            "status": "completed",
        },
        {
            "content": "Implement task ledger",
            "active_form": "implementing task ledger",
            "status": "in_progress",
        },
        {
            "content": "Run recorded E2E",
            "active_form": "running recorded E2E",
            "status": "pending",
        },
    ]))

    assert ledger.has_items() is True
    assert ledger.has_in_progress() is True
    assert ledger.open_count() == 2
    assert ledger.completed_count() == 1
    assert ledger.in_progress_task() is not None

    card = build_task_card_output(ledger)
    prompt = build_task_prompt_section(ledger)
    result = build_task_tool_result(ledger)
    payload = task_items_to_payload(ledger.items)
    nudge = build_task_continue_nudge(ledger)

    assert "[in progress] Implement task ledger" in card
    assert "[in_progress] Implement task ledger" in prompt
    assert "<active-task>implementing task ledger</active-task>" in result
    assert payload[1]["status"] == "in_progress"
    assert "implementing task ledger" in nudge


def test_task_prompt_and_nudge_handle_empty_or_inactive_ledgers() -> None:
    empty = SessionTaskLedger()
    empty_prompt = build_task_prompt_section(empty)
    assert "No current task ledger." in empty_prompt
    assert "next action should usually be a `task` call" in empty_prompt
    assert "same response" in empty_prompt
    assert build_task_continue_nudge(empty) == ""

    pending_only = SessionTaskLedger()
    pending_only.replace(parse_task_items([
        {"content": "Wait for user answer", "status": "pending"},
    ]))
    assert build_task_continue_nudge(pending_only) == ""
