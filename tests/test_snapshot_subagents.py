"""Renderer checks for subagent status chrome."""

from __future__ import annotations

from pathlib import Path

from successor.chat import SuccessorChat, _Message
from successor.profiles import Profile
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain
from successor.subagents.cards import SubagentToolCard
from successor.subagents.manager import SubagentTaskCounts


class _FakeManager:
    def counts(self) -> SubagentTaskCounts:
        return SubagentTaskCounts(running=1, completed=1)

    def drain_notifications(self) -> list:
        return []

    def has_active_tasks(self) -> bool:
        return True

    def reconfigure(self, *, max_model_tasks: int) -> bool:
        return True


def test_title_bar_shows_background_task_badge(temp_config_dir: Path) -> None:
    chat = SuccessorChat(profile=Profile(name="snapshot-subagents"))
    chat._subagent_manager = _FakeManager()
    grid = Grid(30, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "tasks 1/2" in plain


def test_subagent_card_renders_in_chat_body(temp_config_dir: Path) -> None:
    chat = SuccessorChat(profile=Profile(name="snapshot-subagents"))
    chat.messages = [
        _Message("user", "delegate this"),
        _Message("successor", "", display_text=""),
        _Message(
            "tool",
            "",
            subagent_card=SubagentToolCard(
                task_id="t001",
                name="version-audit",
                directive="audit the version files and report what is true",
                tool_call_id="call_sub_1",
                spawn_result=(
                    "<subagent-spawned>\n"
                    "<task_id>t001</task_id>\n"
                    "<name>version-audit</name>\n"
                    "<status>queued</status>\n"
                    "</subagent-spawned>"
                ),
            ),
        ),
    ]
    grid = Grid(30, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "subagent" in plain
    assert "t001" in plain
    assert "version-audit" in plain
    assert "queued" in plain
    assert "╭" in plain
    assert "╯" in plain
    assert "hblbl" not in plain
    assert "tlblbl" not in plain
