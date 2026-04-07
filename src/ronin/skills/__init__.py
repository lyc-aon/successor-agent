"""Skills — markdown instructions the agent can load into context.

Skills are pure data: a name, a description (when to use it), and a
markdown body of instructions or examples. They're loaded from `*.md`
files via the same Registry pattern themes and profiles use, and they
follow the same frontmatter format Claude Code skills use, so a user's
existing `~/.claude/skills/*.md` library can be symlinked or copied
into `~/.config/ronin/skills/` and immediately work.

Phase 5 is loader-only: skills are inventoried (`rn skills list`,
`SKILL_REGISTRY.all()`) but NOT yet wired into the chat. How skill
bodies reach the model — always-on prepend vs on-demand tool — is a
separate decision pinned to hands-on time with the local model.

Public surface:
    Skill              dataclass with name, description, body, source_path
    parse_skill_file   Path → Skill (used by the registry parser)
    SKILL_REGISTRY     the Registry[Skill] singleton
    get_skill(name)    convenience lookup
    all_skills()       list of every loaded skill
"""

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
    "get_skill",
    "parse_skill_file",
]
