"""Chat integration coverage for the internal verification contract tool."""

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


def test_verify_tool_dispatch_updates_session_contract(temp_config_dir: Path) -> None:
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=_MockClient(),
    )
    chat.messages = []

    assert chat._dispatch_native_tool_calls([
        {
            "id": "call_verify_1",
            "name": "verify",
            "arguments": {
                "items": [
                    {
                        "claim": "The page loads without a blank viewport",
                        "evidence": "browser open plus screenshot inspection",
                        "status": "in_progress",
                    },
                    {
                        "claim": "Console remains clean",
                        "evidence": "browser console_errors output",
                        "status": "pending",
                    },
                ]
            },
            "raw_arguments": '{"items":[{"claim":"The page loads without a blank viewport","evidence":"browser open plus screenshot inspection","status":"in_progress"},{"claim":"Console remains clean","evidence":"browser console_errors output","status":"pending"}]}',
        }
    ])

    assert chat._verification_ledger.has_items() is True
    active = chat._verification_ledger.in_progress_item()
    assert active is not None
    assert active.claim == "The page loads without a blank viewport"

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "verify"
    assert cards[0].tool_call_id == "call_verify_1"
    assert "Updated the session verification contract." in cards[0].output
    assert "<verification-contract>" in (cards[0].api_content_override or "")

    chat._trace.close()
    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "verification_contract_updated" for event in events)
