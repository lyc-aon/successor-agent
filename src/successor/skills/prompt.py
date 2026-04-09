"""Prompt helpers for skill discovery and invocation."""

from __future__ import annotations

from .skill import Skill, get_skill


SKILL_BUDGET_CONTEXT_PERCENT = 0.01
CHARS_PER_TOKEN = 4
DEFAULT_CHAR_BUDGET = 8_000
MAX_LISTING_DESC_CHARS = 250
RECOMMENDED_SKILLS_BY_TOOL: dict[str, tuple[str, ...]] = {
    "holonet": ("holonet-research", "biomedical-research"),
    "browser": ("browser-operator", "browser-verifier"),
    "vision": ("vision-inspector",),
}
ALWAYS_ON_SKILL_HINTS: dict[str, str] = {
    "browser-operator": (
        "For general live browser interaction, call the `skill` tool with "
        "`browser-operator` before the first browser action. Open once, "
        "inspect if unsure, and avoid broad exploratory clicking."
    ),
    "browser-verifier": (
        "For local app verification, QA, or browser-driven bug reproduction, "
        "call the `skill` tool with `browser-verifier` before the first "
        "browser action. Open once, inspect if unsure, take the smallest "
        "proving action, and stop after the behavior is verified or falsified."
    ),
    "holonet-research": (
        "For general web research with API-backed sources, call the `skill` "
        "tool with `holonet-research` before using `holonet`."
    ),
    "biomedical-research": (
        "For literature/trial research questions, call the `skill` tool with "
        "`biomedical-research` before using `holonet`."
    ),
    "vision-inspector": (
        "For screenshot-based inspection, visual QA, layout checks, or design "
        "polish, call the `skill` tool with `vision-inspector` before using "
        "`vision`."
    ),
}


def enabled_profile_skills(
    skill_names: list[str] | tuple[str, ...],
    *,
    enabled_tools: list[str] | tuple[str, ...],
) -> list[Skill]:
    """Resolve profile-selected skills that are usable this turn."""
    tools = {name.strip().lower() for name in enabled_tools}
    resolved: list[Skill] = []
    seen: set[str] = set()
    for raw_name in skill_names:
        name = raw_name.strip().lower()
        if not name or name in seen:
            continue
        skill = get_skill(name)
        if skill is None:
            continue
        if skill.allowed_tools and not set(skill.allowed_tools).issubset(tools):
            continue
        resolved.append(skill)
        seen.add(skill.name)
    return resolved


def recommended_skills_for_tools(
    enabled_tools: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Built-in skills that pair well with the currently enabled tools."""
    tools = {name.strip().lower() for name in enabled_tools}
    out: list[str] = []
    seen: set[str] = set()
    for tool_name, skill_names in RECOMMENDED_SKILLS_BY_TOOL.items():
        if tool_name not in tools:
            continue
        for skill_name in skill_names:
            if skill_name in seen or get_skill(skill_name) is None:
                continue
            out.append(skill_name)
            seen.add(skill_name)
    return tuple(out)


def listing_char_budget(context_window_tokens: int | None) -> int:
    if isinstance(context_window_tokens, int) and context_window_tokens > 0:
        return max(
            1_000,
            int(context_window_tokens * CHARS_PER_TOKEN * SKILL_BUDGET_CONTEXT_PERCENT),
        )
    return DEFAULT_CHAR_BUDGET


def skill_listing_description(skill: Skill) -> str:
    """Compact description used in the system-prompt discovery list."""
    bits: list[str] = []
    if skill.description:
        bits.append(skill.description.strip())
    if skill.when_to_use:
        bits.append(skill.when_to_use.strip())
    if skill.allowed_tools:
        bits.append(f"tools: {', '.join(skill.allowed_tools)}")
    text = " - ".join(bit for bit in bits if bit)
    if len(text) > MAX_LISTING_DESC_CHARS:
        return text[: MAX_LISTING_DESC_CHARS - 1].rstrip() + "…"
    return text


def format_skill_listing(
    skills: list[Skill],
    *,
    context_window_tokens: int | None,
) -> str:
    """Format a compact discovery-only skill listing within a char budget."""
    if not skills:
        return ""
    budget = listing_char_budget(context_window_tokens)
    entries = [f"- {skill.name}: {skill_listing_description(skill)}" for skill in skills]
    out: list[str] = []
    used = 0
    for entry in entries:
        entry_len = len(entry) + (1 if out else 0)
        if out and used + entry_len > budget:
            break
        out.append(entry)
        used += entry_len
    return "\n".join(out)


def build_skill_discovery_section(
    skills: list[Skill],
    *,
    context_window_tokens: int | None,
) -> str:
    """System-prompt section that teaches the model which skills exist."""
    if not skills:
        return ""
    listing = format_skill_listing(
        skills,
        context_window_tokens=context_window_tokens,
    )
    if not listing:
        return ""
    return (
        "## Available Skills\n\n"
        "The following skills are enabled for this profile. If one clearly "
        "matches the user's request, call the `skill` tool before you use "
        "other tools or answer from memory. The `skill` tool loads the full "
        "instructions on demand, so this list is discovery-only.\n\n"
        f"{listing}"
    )


def build_skill_hint_section(skills: list[Skill]) -> str:
    """Compact always-on routing hints for high-leverage enabled skills."""
    lines: list[str] = []
    for skill in skills:
        hint = ALWAYS_ON_SKILL_HINTS.get(skill.name)
        if not hint:
            continue
        lines.append(f"- {skill.name}: {hint}")
    if not lines:
        return ""
    return (
        "## Skill Routing Hints\n\n"
        "These are short always-on summaries for enabled skills. Load the "
        "matching skill with the `skill` tool when the task clearly fits.\n\n"
        + "\n".join(lines)
    )


def build_skill_tool_result(
    skill: Skill,
    *,
    task: str,
    source: str = "",
) -> str:
    """Full model-facing payload returned by the native `skill` tool."""
    parts = [
        "<skill-loaded>",
        f"<name>{skill.name}</name>",
    ]
    if source:
        parts.append(f"<source>{source}</source>")
    if skill.description:
        parts.append(f"<description>{skill.description}</description>")
    if skill.when_to_use:
        parts.append(f"<when-to-use>{skill.when_to_use}</when-to-use>")
    if skill.allowed_tools:
        parts.append(f"<allowed-tools>{', '.join(skill.allowed_tools)}</allowed-tools>")
    if task:
        parts.append(f"<task>{task}</task>")
    parts.extend([
        "<instructions>",
        skill.body.strip(),
        "</instructions>",
        "</skill-loaded>",
    ])
    return "\n".join(parts)


def build_skill_card_output(skill: Skill, *, task: str, source: str = "") -> str:
    """Short user-facing summary shown inside the rendered tool card."""
    lines = [f"Loaded skill `{skill.name}`."]
    if skill.description:
        lines.append(skill.description)
    if skill.allowed_tools:
        lines.append(f"Allowed tools: {', '.join(skill.allowed_tools)}")
    if task:
        lines.append(f"Task: {task}")
    if source:
        lines.append(f"Source: {source}")
    return "\n".join(lines)


def build_skill_reuse_result(skill_name: str, *, task: str) -> str:
    """Model-facing payload for a duplicate skill invocation."""
    lines = [
        "<skill-already-loaded>",
        f"<name>{skill_name}</name>",
    ]
    if task:
        lines.append(f"<task>{task}</task>")
    lines.extend([
        "<note>This skill is already loaded earlier in the conversation. "
        "Reuse those instructions instead of loading it again.</note>",
        "</skill-already-loaded>",
    ])
    return "\n".join(lines)
