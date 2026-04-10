"""Chat/runtime coverage for reserved local-port refusal behavior."""

from __future__ import annotations

from pathlib import Path

from successor.bash import resolve_bash_config
from successor.chat import SuccessorChat
from successor.profiles import Profile


class _MockClient:
    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url
        self.model = "mock-model"

    def stream_chat(self, messages, **kwargs):  # noqa: ARG002
        raise AssertionError("stream_chat should not be called in this test")


def test_reserved_provider_port_reclaim_is_refused_even_in_yolo_mode(
    temp_config_dir: Path,  # noqa: ARG001
) -> None:
    profile = Profile(
        name="agent",
        tools=("bash",),
        provider={"base_url": "http://127.0.0.1:8080"},
        tool_config={"bash": {"allow_dangerous": True}},
    )
    chat = SuccessorChat(
        profile=profile,
        client=_MockClient(base_url="http://127.0.0.1:8080"),
    )
    chat.messages = []

    ran = chat._spawn_bash_runner(
        "lsof -ti:8080 | xargs -r kill",
        bash_cfg=resolve_bash_config(chat.profile),
        tool_call_id="call_bash_1",
    )

    assert ran is False
    assert not chat._running_tools

    tool_msgs = [msg for msg in chat.messages if msg.tool_card is not None]
    assert len(tool_msgs) == 1
    card = tool_msgs[0].tool_card
    assert card is not None
    assert card.risk == "dangerous"
    assert card.executed is False
    assert card.tool_call_id == "call_bash_1"
    assert card.raw_command == "lsof -ti:8080 | xargs -r kill"

    successor_msgs = [msg.raw_text for msg in chat.messages if msg.role == "successor"]
    assert any("reserved local port 8080" in text for text in successor_msgs)
    assert any("Pick a different free port instead" in text for text in successor_msgs)
