"""Tests for the skill loader, frontmatter parser, and registry.

Same hermetic pattern as the theme/profile tests — real temp dirs,
real *.md files, no mocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from successor.skills import (
    SKILL_REGISTRY,
    Skill,
    all_skills,
    get_skill,
    parse_skill_file,
)
from successor.skills.skill import _split_frontmatter


# ─── _split_frontmatter (unit-level test of the parser internals) ───


def test_split_frontmatter_happy_path() -> None:
    text = (
        "---\n"
        "name: test-skill\n"
        "description: a test skill\n"
        "---\n"
        "\n"
        "# Body\n"
        "\n"
        "Some content here.\n"
    )
    fm, body = _split_frontmatter(text)
    assert fm is not None
    assert fm == {"name": "test-skill", "description": "a test skill"}
    assert body.startswith("# Body")


def test_split_frontmatter_no_block_returns_none() -> None:
    text = "# Just a markdown file\n\nNo frontmatter here.\n"
    fm, body = _split_frontmatter(text)
    assert fm is None
    assert body == text


def test_split_frontmatter_unclosed_block_returns_none() -> None:
    """An opening --- without a closing --- isn't a valid frontmatter block."""
    text = "---\nname: test\n# Body without close\n"
    fm, _body = _split_frontmatter(text)
    assert fm is None


def test_split_frontmatter_skips_comments_and_blanks() -> None:
    text = (
        "---\n"
        "# this is a comment\n"
        "\n"
        "name: test\n"
        "# another comment\n"
        "description: hi\n"
        "---\n"
        "body\n"
    )
    fm, _body = _split_frontmatter(text)
    assert fm == {"name": "test", "description": "hi"}


def test_split_frontmatter_lowercases_keys() -> None:
    text = "---\nName: test\nDescription: hi\n---\nbody\n"
    fm, _body = _split_frontmatter(text)
    assert fm == {"name": "test", "description": "hi"}


def test_split_frontmatter_drops_leading_body_blank() -> None:
    """A blank line right after the closing --- is dropped from the body."""
    text = "---\nname: test\n---\n\nactual content\n"
    _fm, body = _split_frontmatter(text)
    assert body == "actual content"


# ─── parse_skill_file ───


def test_parse_minimal_skill(tmp_path: Path) -> None:
    p = tmp_path / "minimal.md"
    p.write_text(
        "---\n"
        "name: minimal\n"
        "---\n"
        "body\n"
    )
    skill = parse_skill_file(p)
    assert skill is not None
    assert skill.name == "minimal"
    assert skill.description == ""
    assert skill.body == "body"


def test_parse_full_skill(tmp_path: Path) -> None:
    p = tmp_path / "full.md"
    p.write_text(
        "---\n"
        "name: TheBigOne\n"
        "description: when to use this skill\n"
        "---\n"
        "\n"
        "# Heading\n"
        "\n"
        "Body content with multiple\n"
        "lines.\n"
    )
    skill = parse_skill_file(p)
    assert skill is not None
    assert skill.name == "thebigone"  # lowercased
    assert skill.description == "when to use this skill"
    assert "# Heading" in skill.body
    assert "multiple" in skill.body


def test_parse_skill_without_frontmatter_returns_none(tmp_path: Path) -> None:
    """A markdown file without frontmatter is silently skipped."""
    p = tmp_path / "readme.md"
    p.write_text("# README\n\nNot a skill.\n")
    assert parse_skill_file(p) is None


def test_parse_skill_missing_name_raises(tmp_path: Path) -> None:
    """A frontmatter block without a name field is malformed."""
    p = tmp_path / "broken.md"
    p.write_text(
        "---\n"
        "description: missing name\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(ValueError, match="missing 'name'"):
        parse_skill_file(p)


def test_estimated_tokens_is_chars_over_4(tmp_path: Path) -> None:
    p = tmp_path / "sized.md"
    body = "a" * 400  # ~100 tokens by the chars/4 heuristic
    p.write_text(
        "---\n"
        "name: sized\n"
        "---\n"
        + body + "\n"
    )
    skill = parse_skill_file(p)
    assert skill is not None
    assert skill.estimated_tokens == len(skill.body) // 4


def test_source_path_is_absolute(tmp_path: Path) -> None:
    p = tmp_path / "src.md"
    p.write_text("---\nname: src\n---\nbody\n")
    skill = parse_skill_file(p)
    assert skill is not None
    assert Path(skill.source_path).is_absolute()


# ─── SKILL_REGISTRY ───


def test_builtin_successor_rendering_skill_loads(temp_config_dir: Path) -> None:
    """The bundled successor-rendering-pattern skill is in the registry."""
    SKILL_REGISTRY.reload()
    skill = get_skill("successor-rendering-pattern")
    assert skill is not None
    assert skill.name == "successor-rendering-pattern"
    assert "diff.py" in skill.body  # mentions the One Rule
    assert SKILL_REGISTRY.source_of("successor-rendering-pattern") == "builtin"


def test_user_skill_loads(temp_config_dir: Path) -> None:
    user_dir = temp_config_dir / "skills"
    user_dir.mkdir()
    (user_dir / "my-skill.md").write_text(
        "---\n"
        "name: my-skill\n"
        "description: a custom user skill\n"
        "---\n"
        "Custom body.\n"
    )

    SKILL_REGISTRY.reload()
    skill = get_skill("my-skill")
    assert skill is not None
    assert skill.description == "a custom user skill"
    assert SKILL_REGISTRY.source_of("my-skill") == "user"


def test_user_skill_overrides_builtin(temp_config_dir: Path) -> None:
    user_dir = temp_config_dir / "skills"
    user_dir.mkdir()
    (user_dir / "successor-rendering-pattern.md").write_text(
        "---\n"
        "name: successor-rendering-pattern\n"
        "description: user override\n"
        "---\n"
        "OVERRIDDEN BODY\n"
    )

    SKILL_REGISTRY.reload()
    skill = get_skill("successor-rendering-pattern")
    assert skill is not None
    assert skill.description == "user override"
    assert "OVERRIDDEN BODY" in skill.body
    assert SKILL_REGISTRY.source_of("successor-rendering-pattern") == "user"


def test_broken_user_skill_doesnt_block_builtin(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    user_dir = temp_config_dir / "skills"
    user_dir.mkdir()
    (user_dir / "broken.md").write_text(
        "---\n"
        "description: missing name field\n"
        "---\n"
        "body\n"
    )

    SKILL_REGISTRY.reload()
    # The builtin still loaded
    assert get_skill("successor-rendering-pattern") is not None
    # The broken file was skipped with a warning
    assert get_skill("broken") is None
    captured = capsys.readouterr()
    assert "broken.md" in captured.err


def test_readme_in_skills_dir_is_silently_skipped(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A README dropped into skills/ doesn't error or warn."""
    user_dir = temp_config_dir / "skills"
    user_dir.mkdir()
    (user_dir / "README.md").write_text("# Notes about my skills\n")

    SKILL_REGISTRY.reload()
    captured = capsys.readouterr()
    assert "README.md" not in captured.err  # silent skip


def test_all_skills_returns_loaded(temp_config_dir: Path) -> None:
    SKILL_REGISTRY.reload()
    skills = all_skills()
    assert len(skills) >= 1  # at least the builtin
    names = [s.name for s in skills]
    assert "successor-rendering-pattern" in names
