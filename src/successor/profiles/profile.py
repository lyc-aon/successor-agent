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
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_chat_config, save_chat_config
from ..loader import Registry


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
      skills            ordered tuple of skill names (not yet wired into
                        the chat — phase 5 ships the skill loader)
      tools             ordered tuple of tool names (not yet wired into
                        the chat — phase 6 ships the tool registry)
      tool_config       per-tool configuration dict (passed to tools at
                        dispatch time when the agent loop lands)
      intro_animation   name of an intro animation to play before chat,
                        or None to skip. "successor" plays the bundled
                        braille emergence sequence for ~4 seconds.
      chat_intro_art    name of a braille frame to use as the chat's
                        empty-state hero panel, or None to skip the
                        hero entirely. "successor" loads the bundled
                        title portrait. Custom art: drop a braille
                        text file at ~/.config/successor/art/<name>.txt
                        and reference it as `<name>`, or pass an
                        absolute path.
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
