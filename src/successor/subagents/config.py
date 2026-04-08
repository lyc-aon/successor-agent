"""Subagent configuration for profile.json.

The first shipping pass keeps the config deliberately small and only
exposes knobs the runtime actually honors today:

  - enabled           gates manual `/fork` plus the model-visible
                      `subagent` tool
  - strategy          how background model lanes are scheduled:
                      serial, slot-aware llama.cpp, or manual width
  - max_model_tasks   queue width for background child chats
  - notify_on_finish  whether the parent chat gets completion toasts
  - timeout_s         hard wall-clock limit per child task
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUBAGENT_STRATEGIES = ("serial", "slots", "manual")


@dataclass(frozen=True, slots=True)
class SubagentConfig:
    """Per-profile background-subagent settings.

    Manual `/fork` works whenever `enabled` is on. The model-visible
    `subagent` tool additionally requires `notify_on_finish` so the
    parent chat can receive a later completion event.
    """

    enabled: bool = True
    strategy: str = "serial"
    max_model_tasks: int = 1
    notify_on_finish: bool = True
    timeout_s: float = 900.0

    def __post_init__(self) -> None:
        if self.strategy not in SUBAGENT_STRATEGIES:
            raise ValueError(
                f"strategy must be one of {', '.join(SUBAGENT_STRATEGIES)}, "
                f"got {self.strategy!r}"
            )
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
            "strategy": self.strategy,
            "max_model_tasks": self.max_model_tasks,
            "notify_on_finish": self.notify_on_finish,
            "timeout_s": self.timeout_s,
        }

    def effective_max_model_tasks(self, client: object | None) -> int:
        """Resolve the live background-model width for this provider."""
        requested = max(1, int(self.max_model_tasks))
        if self.strategy == "serial":
            return 1
        if self.strategy == "manual":
            return requested
        detect = getattr(client, "detect_runtime_capabilities", None)
        if not callable(detect):
            return 1
        try:
            capabilities = detect()
        except Exception:
            return 1
        usable = getattr(capabilities, "usable_background_slots", None)
        if isinstance(usable, int) and usable > 0:
            return min(requested, usable)
        return 1

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
        strategy = data.get("strategy")
        if isinstance(strategy, str):
            normalized = strategy.strip().lower()
            if normalized in SUBAGENT_STRATEGIES:
                kwargs["strategy"] = normalized

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
                if k in ("enabled", "notify_on_finish", "strategy")
            }
            try:
                return cls(**safe_kwargs)
            except ValueError:
                return defaults
