"""Skill dataclass + frontmatter parser + registry.

Skills are markdown files with YAML-style frontmatter at the top:

    ---
    name: ronin-rendering-pattern
    description: Use this skill when building any terminal UI...
    ---

    # Skill body

    Body markdown here. The whole document below the closing `---`
    becomes the skill's body.

The frontmatter parser is intentionally tiny — line-by-line, no nested
keys, no quoted values, no multi-line values, no anchors. This matches
how Claude Code skills are written in practice (flat key: value pairs)
and means we don't need PyYAML as a dependency. Pure stdlib.

Files without a recognizable frontmatter block are silently skipped
(parser returns None) — that's how README.md and other dropped-in
markdown files coexist with skills in the same directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..loader import Registry


@dataclass(frozen=True, slots=True)
class Skill:
    """One named skill — markdown body + frontmatter metadata.

    Fields:
      name           lowercase identifier (matches the frontmatter `name`)
      description    one-line "when to use this skill" hint
      body           the markdown content below the frontmatter block
      source_path    absolute path to the source file (for `rn skills list`
                     and debugging)
    """

    name: str
    description: str
    body: str
    source_path: str

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate using the standard chars/4 heuristic.

        Used by `rn skills list` to show users approximately how much
        context window each skill consumes if always-on. Not exact —
        the real number depends on the tokenizer — but accurate enough
        for sizing decisions.
        """
        return max(0, len(self.body) // 4)


def parse_skill_file(path: Path) -> Skill | None:
    """Parse a markdown file with YAML-style frontmatter into a Skill.

    Returns None for files without valid frontmatter (README.md,
    CHANGELOG.md, etc. dropped into the same directory).

    Raises ValueError for files that LOOK like skills (have a `---`
    frontmatter block) but are malformed (missing name, etc.). The
    registry catches the exception and emits a stderr warning.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"read failed: {exc}") from exc

    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        # No frontmatter block at all — silently skip (not a skill).
        return None

    name = frontmatter.get("name")
    if not name:
        raise ValueError("frontmatter missing 'name' field")
    description = frontmatter.get("description", "")

    return Skill(
        name=name.strip().lower(),
        description=description.strip(),
        body=body,
        source_path=str(path.resolve()),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """Extract a `--- ... ---` frontmatter block from the start of text.

    Returns (frontmatter_dict, body_text). frontmatter_dict is None if
    no frontmatter block was found at the very start of the file.

    Parser rules (intentionally minimal):
      - The file MUST start with `---\\n` (no leading whitespace)
      - Each frontmatter line is `key: value` (colon-space delimiter)
      - Empty lines and comment lines (starting with #) are skipped
      - The block ends at the next `---` line on its own
      - Body is everything after that closing `---`
      - Last value wins on duplicate keys (no merging)
    """
    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != "---":
        return (None, text)

    # Find the closing --- line
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx < 0:
        return (None, text)

    frontmatter: dict[str, str] = {}
    for line in lines[1:close_idx]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        frontmatter[key.strip().lower()] = value.strip()

    # Body is everything after the closing --- line. Preserve the
    # original line endings by joining with \n (the splitlines() call
    # above stripped them).
    body_lines = lines[close_idx + 1 :]
    # Drop one leading blank line if present so the body doesn't begin
    # with a stray newline that the user didn't intend.
    if body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)
    return (frontmatter, body)


# ─── Registry ───


SKILL_REGISTRY: Registry[Skill] = Registry[Skill](
    kind="skills",
    file_glob="*.md",
    parser=parse_skill_file,
    description="skill",
)


def get_skill(name: str) -> Skill | None:
    """Look up a skill by name. Triggers loader if not yet loaded."""
    return SKILL_REGISTRY.get(name)


def all_skills() -> list[Skill]:
    """Return every loaded skill in load order."""
    return SKILL_REGISTRY.all()
