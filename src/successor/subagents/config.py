"""Subagent configuration for profile.json.

The first shipping pass keeps the config deliberately small and only
exposes knobs the runtime actually honors today:

  - enabled           gates manual `/fork` plus the model-visible
                      `subagent` tool
  - max_model_tasks   queue width for background child chats
  - notify_on_finish  whether the parent chat gets completion toasts
  - timeout_s         hard wall-clock limit per child task

The future slot-aware scheduler can grow this surface later without
breaking the JSON shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SubagentConfig:
    """Per-profile background-subagent settings.

    Manual `/fork` works whenever `enabled` is on. The model-visible
    `subagent` tool additionally requires `notify_on_finish` so the
    parent chat can receive a later completion event.
    """

    enabled: bool = True
    max_model_tasks: int = 1
    notify_on_finish: bool = True
    timeout_s: float = 900.0

    def __post_init__(self) -> None:
        if self.max_model_tasks < 1:
            raise ValueError(
                f"max_model_tasks must be >= 1, got {self.max_model_tasks}"
            )
        if self.timeout_s <= 0:
            raise ValueError(
                f"timeout_s must be > 0, got {self.timeout_s}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_model_tasks": self.max_model_tasks,
            "notify_on_finish": self.notify_on_finish,
            "timeout_s": self.timeout_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SubagentConfig":
        """Lenient JSON parsing mirroring the profile loader policy."""
        if not isinstance(data, dict):
            return cls()

        defaults = cls()
        kwargs: dict[str, Any] = {}

        if isinstance(data.get("enabled"), bool):
            kwargs["enabled"] = data["enabled"]
        if isinstance(data.get("notify_on_finish"), bool):
            kwargs["notify_on_finish"] = data["notify_on_finish"]

        max_model_tasks = data.get("max_model_tasks")
        if isinstance(max_model_tasks, int) and not isinstance(max_model_tasks, bool):
            if max_model_tasks >= 1:
                kwargs["max_model_tasks"] = max_model_tasks

        timeout_s = data.get("timeout_s")
        if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool):
            timeout_value = float(timeout_s)
            if timeout_value > 0:
                kwargs["timeout_s"] = timeout_value

        try:
            return cls(**kwargs)
        except ValueError:
            safe_kwargs = {
                k: v for k, v in kwargs.items()
                if k in ("enabled", "notify_on_finish")
            }
            try:
                return cls(**safe_kwargs)
            except ValueError:
                return defaults
