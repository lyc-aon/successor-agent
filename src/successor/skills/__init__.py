"""Skills — markdown instructions the agent can load into context.

Skills are pure data: a name, a description (when to use it), and a
markdown body of instructions or examples. They're loaded from `*.md`
files via the same Registry pattern themes and profiles use, and they
follow the same frontmatter format Claude Code skills use, so a user's
existing `~/.claude/skills/*.md` library can be symlinked or copied
into `~/.config/successor/skills/` and immediately work.

Skills are now wired into chat through an internal native `skill`
tool: the system prompt gets a compact discovery list, and the full
skill body is only loaded on demand when the model invokes that tool.

Public surface:
    Skill              dataclass with name, description, body, source_path
    parse_skill_file   Path → Skill (used by the registry parser)
    SKILL_REGISTRY     the Registry[Skill] singleton
    get_skill(name)    convenience lookup
    all_skills()       list of every loaded skill
"""

from .prompt import (
    build_skill_card_output,
    build_skill_discovery_section,
    build_skill_reuse_result,
    build_skill_tool_result,
    enabled_profile_skills,
    format_skill_listing,
    recommended_skills_for_tools,
)
from .skill import (
    SKILL_REGISTRY,
    Skill,
    all_skills,
    get_skill,
    parse_skill_file,
)

__all__ = [
    "SKILL_REGISTRY",
    "Skill",
    "all_skills",
    "build_skill_card_output",
    "build_skill_discovery_section",
    "build_skill_reuse_result",
    "build_skill_tool_result",
    "enabled_profile_skills",
    "format_skill_listing",
    "get_skill",
    "parse_skill_file",
    "recommended_skills_for_tools",
]
