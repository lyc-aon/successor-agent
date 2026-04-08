"""Card model for model-visible subagent tool calls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubagentToolCard:
    """Structured record of a spawned background subagent."""

    task_id: str
    directive: str
    tool_call_id: str
    spawn_result: str
    name: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.task_id
