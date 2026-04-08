"""Tests for normal chat session tracing + shutdown cleanup."""

from __future__ import annotations

import json
import time
from pathlib import Path

from successor.bash import resolve_bash_config
from successor.chat import SuccessorChat


def _trace_events(root: Path) -> list[dict]:
    trace_files = sorted((root / "logs").glob("*.jsonl"))
    assert trace_files, "expected at least one trace file"
    lines = trace_files[-1].read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _wait_for_runners(chat: SuccessorChat, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while chat._running_tools and time.monotonic() < deadline:
        chat._pump_running_tools()
        time.sleep(0.01)
    assert not chat._running_tools, "runner batch did not settle in time"


def test_normal_chat_writes_runner_trace_events(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    try:
        bash_cfg = resolve_bash_config(chat.profile)
        assert chat._spawn_bash_runner("echo hello trace", bash_cfg=bash_cfg)
        _wait_for_runners(chat)
        chat._trace.close()
    finally:
        chat._shutdown_runtime_for_exit()
        chat._trace.close()

    events = _trace_events(temp_config_dir)
    types = [event["type"] for event in events]
    assert "session_start" in types
    assert "bash_spawn" in types
    assert "bash_runner_started" in types
    assert "bash_runner_finished" in types
    finished = [event for event in events if event["type"] == "bash_runner_finished"]
    assert finished[-1]["exit_code"] == 0
    assert "hello trace" in finished[-1]["stdout_excerpt"]


def test_shutdown_runtime_cancels_inflight_runner(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    try:
        bash_cfg = resolve_bash_config(chat.profile)
        assert chat._spawn_bash_runner("sleep 5", bash_cfg=bash_cfg)
        time.sleep(0.05)
        chat._shutdown_runtime_for_exit()
        chat._trace.close()
    finally:
        chat._shutdown_runtime_for_exit()
        chat._trace.close()

    assert all(msg.running_tool is None for msg in chat.messages if msg.tool_card is not None)
    cancelled = [
        msg.tool_card for msg in chat.messages
        if msg.tool_card is not None and msg.tool_card.exit_code is not None
    ]
    assert cancelled, "expected a finalized cancelled tool card"
    assert any(card.stderr and "cancelled" in card.stderr for card in cancelled)

    events = _trace_events(temp_config_dir)
    types = [event["type"] for event in events]
    assert "shutdown_cancel_running_tools" in types
    finished = [event for event in events if event["type"] == "bash_runner_finished"]
    assert finished, "expected cancelled runner finalization in trace"
    assert finished[-1]["error"] == "cancelled"
