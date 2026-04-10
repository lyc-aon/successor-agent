"""Chat integration coverage for the internal runbook tool."""

from __future__ import annotations

import json
from pathlib import Path

from successor.chat import SuccessorChat
from successor.profiles import Profile


class _MockClient:
    base_url = "http://mock"
    model = "mock-model"

    def stream_chat(self, messages, **kwargs):  # noqa: ARG002
        raise AssertionError("stream_chat should not be called in this test")


def _trace_events(root: Path) -> list[dict]:
    trace_files = sorted((root / "logs").glob("*.jsonl"))
    assert trace_files, "expected trace files"
    return [
        json.loads(line)
        for line in trace_files[-1].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_runbook_tool_dispatch_updates_session_state_and_records_attempt(
    temp_config_dir: Path,
) -> None:
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=_MockClient(),
    )
    chat.messages = []

    assert chat._dispatch_native_tool_calls([
        {
            "id": "call_runbook_1",
            "name": "runbook",
            "arguments": {
                "objective": "Ship a stable typing game loop",
                "success_definition": "Scripted playthrough reaches game over with correct score and no console errors",
                "scope": ["src/game", "src/ui"],
                "baseline_status": "captured",
                "baseline_summary": "Current build opens but the player stalls after the first wave",
                "active_hypothesis": "Input focus is lost after wave transitions",
                "evaluator": [
                    {
                        "id": "build",
                        "kind": "command",
                        "spec": "npm run build",
                        "pass_condition": "exit 0",
                    }
                ],
                "status": "running",
                "attempt": {
                    "hypothesis": "Locking focus after transitions fixes the stall",
                    "summary": "Build passed and scripted player reached wave three",
                    "decision": "kept",
                    "files_touched": ["src/game/input.ts"],
                },
            },
            "raw_arguments": '{"objective":"Ship a stable typing game loop","success_definition":"Scripted playthrough reaches game over with correct score and no console errors","scope":["src/game","src/ui"],"baseline_status":"captured","baseline_summary":"Current build opens but the player stalls after the first wave","active_hypothesis":"Input focus is lost after wave transitions","evaluator":[{"id":"build","kind":"command","spec":"npm run build","pass_condition":"exit 0"}],"status":"running","attempt":{"hypothesis":"Locking focus after transitions fixes the stall","summary":"Build passed and scripted player reached wave three","decision":"kept","files_touched":["src/game/input.ts"]}}',
        }
    ])

    assert chat._runbook.has_state() is True
    assert chat._runbook.state is not None
    assert chat._runbook.state.objective == "Ship a stable typing game loop"
    assert chat._runbook_attempt_count == 1

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "runbook"
    assert cards[0].tool_call_id == "call_runbook_1"
    assert "Updated the session runbook." in cards[0].output
    assert "<runbook>" in (cards[0].api_content_override or "")

    chat._trace.close()
    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "runbook_updated" for event in events)
    assert any(event["type"] == "experiment_attempt_recorded" for event in events)
