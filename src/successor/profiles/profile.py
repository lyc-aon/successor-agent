"""Profile dataclass + JSON loader + active-profile resolution.

Profiles are pure data, loaded from JSON via the Registry pattern.
Every field except `name` has a sensible default so a minimal profile
file is just `{"name": "minimal"}`.

Validation is lenient on load: a profile that references an unknown
theme or unknown skill is still loaded successfully (the chat resolves
those at activation time and falls back gracefully). The intent is
"never reject a profile at load time for downstream issues" — that
keeps `successor profiles list` and `successor doctor` informative even when one
profile is half-broken.

The one exception is `CompactionConfig`: if a profile sets compaction
percentages that violate the threshold ordering invariant
(`warning_pct > autocompact_pct > blocking_pct`), the parser silently
clamps the values back to safe defaults rather than rejecting the
profile, mirroring the rest of the lenient-load policy. The clamp is
documented on `CompactionConfig.from_dict`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_chat_config, save_chat_config
from ..loader import Registry
from ..subagents.config import SubagentConfig


# ─── CompactionConfig ───
#
# All thresholds are expressed as fractions of the resolved context
# window. The chat builds a runtime ContextBudget from these by
# multiplying each pct by the actual window size, with a hard floor
# from the *_floor fields so tiny windows still get a usable buffer.
#
# Defaults match the pre-config hardcoded values in `chat._agent_budget`
# (window // 16 / 32 / 128) so existing profiles upgrade without any
# behavior change.
#
# Invariant: warning_pct > autocompact_pct > blocking_pct >= 0
# Invariant: every pct is in [0, 1]
# Invariant: floors are non-negative


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """Per-profile autocompaction thresholds and behavior.

    The percentages drive WHEN compaction fires; the floors guarantee
    a usable headroom even at tiny window sizes; the booleans + ints
    drive HOW compaction behaves once it fires.

    Frozen because profiles are immutable. Mutate by constructing a
    new instance via dataclasses.replace().
    """

    # ── Threshold percentages (fraction of total context window) ──
    # Each defines the SIZE of the buffer subtracted from the window
    # to determine when that threshold trips. So:
    #   autocompact fires at:  used >= window - max(floor, window * pct)
    #
    # Example with window=200K and autocompact_pct=0.0625 (1/16):
    #   buffer = max(4_000, 200_000 * 0.0625) = max(4000, 12500) = 12500
    #   autocompact at:  used >= 200_000 - 12500 = 187_500 tokens
    warning_pct: float = 1.0 / 8.0          # 12.5% — show warning pill
    autocompact_pct: float = 1.0 / 16.0     # ~6.25% — fire compaction
    blocking_pct: float = 1.0 / 64.0        # ~1.56% — refuse the API call

    # ── Floors (in tokens) — guarantee a minimum buffer at small window sizes ──
    warning_floor: int = 8_000
    autocompact_floor: int = 4_000
    blocking_floor: int = 1_000

    # ── Behavior ──
    enabled: bool = True
    """When False: autocompact never fires proactively. Reactive PTL
    recovery still works (the loop catches prompt-too-long stream
    errors and compacts in response). The blocking buffer is also
    still honored — disabled means "never compact PROACTIVELY", not
    "let the chat overflow the API limit". Use this for debugging or
    when you want to manually control compaction via /compact."""

    keep_recent_rounds: int = 6
    """How many of the most recent rounds to preserve verbatim past a
    compaction. Lower = more aggressive compaction (smaller post-compact
    log) but less continuity. Higher = better continuity but less room
    saved per compaction."""

    summary_max_tokens: int = 16_000
    """Maximum tokens the summarization model is allowed to produce.
    Caps the output of the summary call. If the model emits longer,
    the stream gets truncated."""

    def __post_init__(self) -> None:
        # Range checks first — these are programmer errors and worth
        # failing loudly at construction time.
        if not 0.0 <= self.warning_pct <= 1.0:
            raise ValueError(
                f"warning_pct must be in [0, 1], got {self.warning_pct}"
            )
        if not 0.0 <= self.autocompact_pct <= 1.0:
            raise ValueError(
                f"autocompact_pct must be in [0, 1], got {self.autocompact_pct}"
            )
        if not 0.0 <= self.blocking_pct <= 1.0:
            raise ValueError(
                f"blocking_pct must be in [0, 1], got {self.blocking_pct}"
            )

        # Ordering invariant: warning fires earliest (largest buffer),
        # autocompact next, blocking latest (smallest buffer).
        if not (self.warning_pct > self.autocompact_pct > self.blocking_pct >= 0):
            raise ValueError(
                f"compaction thresholds out of order: "
                f"warning_pct={self.warning_pct} > "
                f"autocompact_pct={self.autocompact_pct} > "
                f"blocking_pct={self.blocking_pct} >= 0"
            )

        # Floor checks
        if self.warning_floor < 0:
            raise ValueError(
                f"warning_floor must be >= 0, got {self.warning_floor}"
            )
        if self.autocompact_floor < 0:
            raise ValueError(
                f"autocompact_floor must be >= 0, got {self.autocompact_floor}"
            )
        if self.blocking_floor < 0:
            raise ValueError(
                f"blocking_floor must be >= 0, got {self.blocking_floor}"
            )
        if not (self.warning_floor >= self.autocompact_floor >= self.blocking_floor):
            raise ValueError(
                f"compaction floors out of order: "
                f"warning_floor={self.warning_floor} >= "
                f"autocompact_floor={self.autocompact_floor} >= "
                f"blocking_floor={self.blocking_floor}"
            )

        # Behavior checks
        if self.keep_recent_rounds < 1:
            raise ValueError(
                f"keep_recent_rounds must be >= 1, got {self.keep_recent_rounds}"
            )
        if self.summary_max_tokens < 256:
            raise ValueError(
                f"summary_max_tokens must be >= 256, got {self.summary_max_tokens}"
            )

    def buffers_for_window(self, window: int) -> tuple[int, int, int]:
        """Resolve (warning_buffer, autocompact_buffer, blocking_buffer)
        for a given window size, applying both the percentage and the
        floor.

        Used by chat._agent_budget to convert this config into a
        runtime ContextBudget. The returned buffers always satisfy
        the same ordering invariant the config does.
        """
        warning_buf = max(self.warning_floor, int(window * self.warning_pct))
        autocompact_buf = max(self.autocompact_floor, int(window * self.autocompact_pct))
        blocking_buf = max(self.blocking_floor, int(window * self.blocking_pct))

        # The floor system can break the ordering invariant on tiny
        # windows where two floors collapse to the same value. Spread
        # them apart by 1 token at a time so ContextBudget's invariant
        # check still passes. This only matters in pathological cases
        # like window=2000 — real-world windows never hit this branch.
        if not (warning_buf > autocompact_buf > blocking_buf):
            warning_buf = max(warning_buf, autocompact_buf + 2)
            autocompact_buf = max(autocompact_buf, blocking_buf + 1)
            warning_buf = max(warning_buf, autocompact_buf + 1)

        return (warning_buf, autocompact_buf, blocking_buf)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for profile.json round-trip."""
        return {
            "warning_pct": self.warning_pct,
            "autocompact_pct": self.autocompact_pct,
            "blocking_pct": self.blocking_pct,
            "warning_floor": self.warning_floor,
            "autocompact_floor": self.autocompact_floor,
            "blocking_floor": self.blocking_floor,
            "enabled": self.enabled,
            "keep_recent_rounds": self.keep_recent_rounds,
            "summary_max_tokens": self.summary_max_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CompactionConfig:
        """Build a CompactionConfig from a partial JSON dict.

        Missing fields use the defaults. Wrong-typed fields use the
        defaults. If the resulting combination violates an invariant
        (e.g. warning_pct < autocompact_pct), this method silently
        clamps to defaults rather than raising — same lenient-load
        policy the rest of the profile parser uses. The intent is
        "never reject a profile at load time for downstream issues."

        For STRICT validation use `CompactionConfig(**fields)` directly.
        """
        if not isinstance(data, dict):
            return cls()

        defaults = cls()
        kwargs: dict[str, Any] = {}

        # Float fields
        for fname in ("warning_pct", "autocompact_pct", "blocking_pct"):
            v = data.get(fname)
            if isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0:
                kwargs[fname] = float(v)

        # Int fields
        for fname in ("warning_floor", "autocompact_floor", "blocking_floor",
                      "keep_recent_rounds", "summary_max_tokens"):
            v = data.get(fname)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                kwargs[fname] = v

        # Bool field
        if isinstance(data.get("enabled"), bool):
            kwargs["enabled"] = data["enabled"]

        # Try to construct with the parsed values; if any invariant
        # fails (ordering, floor sanity, etc.), fall back to defaults
        # for the offending fields by retrying with progressively
        # narrower kwargs.
        try:
            return cls(**kwargs)
        except ValueError:
            # Invariant violation — try keeping just the behavior fields
            # (those don't affect the threshold ordering)
            safe_kwargs = {
                k: v for k, v in kwargs.items()
                if k in ("enabled", "keep_recent_rounds", "summary_max_tokens")
            }
            try:
                return cls(**safe_kwargs)
            except ValueError:
                return cls()


# Default system prompt for the built-in `default` profile. Tuned for
# the thinking-mode Qwen3.5-27B-Opus-Distilled-v2 model — explicit
# "no markdown headers, no preamble labels" instructions because the
# distilled model otherwise defaults to "Solution:" / "Verification:"
# / checkmark lists from its training data.
_DEFAULT_SYSTEM_PROMPT = (
    "You are successor — a thoughtful, intentional assistant. Speak with "
    "brevity, as if every word costs effort. Reply in a single flowing "
    "paragraph.\n\n"
    "Do not use markdown headers. Do not use bullet lists or numbered lists. "
    'Do not write "Solution:", "Answer:", "Verification:", "Note:", or any '
    "preamble label. Do not use checkmarks. Do not wrap your reply in code "
    "fences unless the user asked for code.\n\n"
    "Think as carefully as you need. When you have finished thinking, simply "
    "give your answer as if speaking aloud. Brevity is honor. When you must "
    "convey several things, weave them into one paragraph rather than "
    "enumerating them."
)


@dataclass(frozen=True, slots=True)
class Profile:
    """One named profile — everything that defines a persona's feel.

    Fields:
      name              registered name (lowercase, used in slash command)
      description       human-readable one-liner for /profile listings
      theme             registered theme name (e.g. "steel", "forge")
      display_mode      "dark" or "light"
      density           "compact", "normal", or "spacious"
      system_prompt     full system prompt sent to the model
      provider          provider config dict for make_provider, or None
                        to use the chat's default LlamaCppClient
      skills            ordered tuple of skill names. The system prompt
                        gets a compact discovery list, and the full
                        skill body is loaded on demand through the
                        internal `skill` tool only when the model asks
                        for it
      tools             ordered tuple of enabled tool names. This
                        controls model-visible native tools plus the
                        tool docs injected into the system prompt
                        (`bash`, `subagent`, `holonet`, `browser`)
      tool_config       per-tool configuration dict. `bash`,
                        `holonet`, and `browser` all read this live;
                        future tools can do the same
      intro_animation   name of an intro animation to play before chat,
                        or None to skip. "successor" plays the bundled
                        braille emergence sequence for ~5 seconds.
      chat_intro_art    name of a braille frame to use as the chat's
                        empty-state hero panel, or None to skip the
                        hero entirely. "successor" loads the bundled
                        hero art (`hero.txt`). Custom art: drop a braille
                        text file at ~/.config/successor/art/<name>.txt
                        and reference it as `<name>`, or pass an
                        absolute path.
      compaction        autocompactor thresholds + behavior, expressed
                        as percentages of the resolved context window
                        with hard floors for tiny windows. Defaults
                        match the historical hardcoded values so
                        existing profiles upgrade transparently. Edit
                        via the wizard's compaction step or the config
                        menu's compaction section.
      subagents         background-task settings shared by manual
                        `/fork` and the model-visible `subagent`
                        tool: enable/disable, scheduling strategy,
                        queue width, completion notifications, and
                        timeout.
    """

    name: str
    description: str = ""
    theme: str = "steel"
    display_mode: str = "dark"
    density: str = "normal"
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT
    provider: dict[str, Any] | None = None
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    tool_config: dict[str, Any] = field(default_factory=dict)
    intro_animation: str | None = None
    chat_intro_art: str | None = None
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    subagents: SubagentConfig = field(default_factory=SubagentConfig)


def parse_profile_file(path: Path) -> Profile | None:
    """Parse a profile JSON file into a Profile.

    Returns None for files that aren't profiles (no `name` field).
    Raises ValueError for files that ARE profiles but are malformed —
    the registry catches the exception and emits a stderr warning
    naming the file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"read failed: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc

    if not isinstance(data, dict):
        raise ValueError("top-level JSON must be an object")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None  # silently skip non-profile files

    # Build kwargs with type-tolerant fallback. Anything missing or
    # wrong-typed reverts to the dataclass default.
    kwargs: dict[str, Any] = {"name": name.strip().lower()}

    if isinstance(data.get("description"), str):
        kwargs["description"] = data["description"]
    if isinstance(data.get("theme"), str):
        kwargs["theme"] = data["theme"].strip().lower()
    if isinstance(data.get("display_mode"), str):
        # Defer normalization to the chat App so the saved value is
        # always exactly what the user wrote — easier to debug than
        # an opaque normalization step.
        kwargs["display_mode"] = data["display_mode"].strip().lower()
    if isinstance(data.get("density"), str):
        kwargs["density"] = data["density"].strip().lower()
    if isinstance(data.get("system_prompt"), str):
        kwargs["system_prompt"] = data["system_prompt"]

    provider_val = data.get("provider")
    if isinstance(provider_val, dict):
        kwargs["provider"] = provider_val
    elif provider_val is None:
        kwargs["provider"] = None

    skills_val = data.get("skills")
    if isinstance(skills_val, list):
        kwargs["skills"] = tuple(s for s in skills_val if isinstance(s, str))

    tools_val = data.get("tools")
    if isinstance(tools_val, list):
        kwargs["tools"] = tuple(t for t in tools_val if isinstance(t, str))

    tc_val = data.get("tool_config")
    if isinstance(tc_val, dict):
        kwargs["tool_config"] = tc_val

    intro_val = data.get("intro_animation")
    if intro_val is None or isinstance(intro_val, str):
        kwargs["intro_animation"] = intro_val

    art_val = data.get("chat_intro_art")
    if art_val is None or isinstance(art_val, str):
        kwargs["chat_intro_art"] = art_val

    # CompactionConfig — lenient: missing field uses defaults, partial
    # field merges with defaults, malformed values use defaults. The
    # CompactionConfig.from_dict classmethod handles all the type
    # checking and invariant clamping.
    compaction_val = data.get("compaction")
    if isinstance(compaction_val, dict):
        kwargs["compaction"] = CompactionConfig.from_dict(compaction_val)
    # If compaction is missing or wrong type, the dataclass default
    # factory provides a fresh CompactionConfig() — no kwarg needed.

    subagents_val = data.get("subagents")
    if isinstance(subagents_val, dict):
        kwargs["subagents"] = SubagentConfig.from_dict(subagents_val)

    return Profile(**kwargs)


# ─── Registry ───


PROFILE_REGISTRY: Registry[Profile] = Registry[Profile](
    kind="profiles",
    file_glob="*.json",
    parser=parse_profile_file,
    description="profile",
)


def get_profile(name: str) -> Profile | None:
    """Look up a profile by name. Triggers loader if not yet loaded."""
    return PROFILE_REGISTRY.get(name)


def all_profiles() -> list[Profile]:
    """Return every loaded profile in load order."""
    return PROFILE_REGISTRY.all()


def next_profile(current: Profile | None) -> Profile:
    """Cycle to the next profile in registry order. Wraps around.

    Returns the synthetic fallback profile if the registry is empty.
    """
    profiles = all_profiles()
    if not profiles:
        return _FALLBACK_PROFILE
    if current is None:
        return profiles[0]
    try:
        idx = profiles.index(current)
    except ValueError:
        # Current profile isn't in the registry (e.g. test fixture)
        return profiles[0]
    return profiles[(idx + 1) % len(profiles)]


# ─── Active profile resolution ───


def get_active_profile() -> Profile:
    """Resolve the currently-active profile from chat config.

    Reads `active_profile` from chat.json. Falls back to "default" if
    that's missing, then to the first registered profile, then to the
    hardcoded fallback. Always returns a valid Profile so callers
    never need to handle None.
    """
    cfg = load_chat_config()
    name = cfg.get("active_profile")
    if isinstance(name, str) and name:
        profile = get_profile(name)
        if profile is not None:
            return profile
    # Fall back to "default"
    default = get_profile("default")
    if default is not None:
        return default
    # Fall back to first registered
    profiles = all_profiles()
    if profiles:
        return profiles[0]
    # Pathological fallback
    return _FALLBACK_PROFILE


def set_active_profile(name: str) -> bool:
    """Persist a new active profile name to chat.json.

    Returns True on successful write. Failures are silent (the chat
    keeps the new profile in memory; it just won't survive restart).
    """
    cfg = load_chat_config()
    cfg["active_profile"] = name
    return save_chat_config(cfg)


# ─── Hardcoded fallback ───
#
# If both the user dir and the package builtin dir fail to load any
# profile, we still need a working Profile so the chat can start. This
# matches the same defensive pattern theme.py uses.

_FALLBACK_PROFILE = Profile(
    name="default",
    description="fallback default — registry unavailable",
)
