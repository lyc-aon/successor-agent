"""Tests for the profile loader, registry, and active-profile resolution.

Covers:
  - parse_profile_file: happy path, missing fields use defaults,
    bad types fall back, malformed JSON raises
  - PROFILE_REGISTRY: built-in default + successor-dev present, user
    override wins on collision, broken user file doesn't block builtins
  - get_active_profile: reads chat.json's active_profile, falls back
    to "default" → first registered → hardcoded fallback
  - set_active_profile: persists to chat.json
  - next_profile: cycles through the registry, wraps, handles None

Uses real temp dirs and real JSON files via temp_config_dir — same
hermetic pattern the theme tests use.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from successor.config import load_chat_config
from successor.profiles import (
    PROFILE_REGISTRY,
    Profile,
    all_profiles,
    get_active_profile,
    get_profile,
    next_profile,
    parse_profile_file,
    set_active_profile,
)


# ─── parse_profile_file ───


def test_parse_minimal_profile(tmp_path: Path) -> None:
    """A profile file with only `name` is valid; everything else defaults."""
    p = tmp_path / "minimal.json"
    p.write_text(json.dumps({"name": "minimal"}))

    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.name == "minimal"
    assert profile.theme == "steel"
    assert profile.display_mode == "dark"
    assert profile.density == "normal"
    assert profile.provider is None
    assert profile.skills == ()
    assert profile.tools == ()
    assert profile.tool_config == {}
    assert profile.intro_animation is None


def test_parse_full_profile(tmp_path: Path) -> None:
    """A profile with every field set parses every field correctly."""
    data = {
        "name": "full",
        "description": "the full monty",
        "theme": "forge",
        "display_mode": "light",
        "density": "spacious",
        "system_prompt": "be terse",
        "provider": {
            "type": "llamacpp",
            "base_url": "http://localhost:8080",
            "model": "local",
            "max_tokens": 16384,
        },
        "skills": ["skill-a", "skill-b"],
        "tools": ["read_file", "bash"],
        "tool_config": {"bash": {"allowed_dirs": ["/tmp"]}},
        "intro_animation": "successor",
    }
    p = tmp_path / "full.json"
    p.write_text(json.dumps(data))

    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.name == "full"
    assert profile.description == "the full monty"
    assert profile.theme == "forge"
    assert profile.display_mode == "light"
    assert profile.density == "spacious"
    assert profile.system_prompt == "be terse"
    assert profile.provider == data["provider"]
    assert profile.skills == ("skill-a", "skill-b")
    assert profile.tools == ("read_file", "bash")
    assert profile.tool_config == {"bash": {"allowed_dirs": ["/tmp"]}}
    assert profile.intro_animation == "successor"


def test_parse_lowercases_name(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    p.write_text(json.dumps({"name": "MixedCase"}))
    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.name == "mixedcase"


def test_parse_skips_file_without_name(tmp_path: Path) -> None:
    """A JSON file without a name field is silently skipped (None)."""
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps({"description": "no name field"}))
    assert parse_profile_file(p) is None


def test_parse_invalid_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_profile_file(p)


def test_parse_top_level_array_raises(tmp_path: Path) -> None:
    p = tmp_path / "array.json"
    p.write_text(json.dumps([]))
    with pytest.raises(ValueError, match="must be an object"):
        parse_profile_file(p)


def test_parse_drops_non_string_skill_entries(tmp_path: Path) -> None:
    """skills field with mixed types — only strings survive."""
    p = tmp_path / "mixed.json"
    p.write_text(json.dumps({
        "name": "mixed",
        "skills": ["good", 42, None, "also-good"],
    }))
    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.skills == ("good", "also-good")


def test_parse_wrong_typed_field_falls_back_to_default(tmp_path: Path) -> None:
    """A wrong-typed field reverts to the default — never crashes."""
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps({
        "name": "wrong",
        "theme": 42,  # should be a string
        "skills": "not a list",  # should be a list
    }))
    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.theme == "steel"  # fell back to default
    assert profile.skills == ()  # fell back to default


# ─── PROFILE_REGISTRY ───


def test_default_profile_is_builtin(temp_config_dir: Path) -> None:
    """The bundled `default` profile is always loadable."""
    PROFILE_REGISTRY.reload()
    default = get_profile("default")
    assert default is not None
    assert default.name == "default"
    assert PROFILE_REGISTRY.source_of("default") == "builtin"


def test_successor_dev_profile_is_builtin_with_intro(temp_config_dir: Path) -> None:
    """The bundled successor-dev profile uses the successor intro animation."""
    PROFILE_REGISTRY.reload()
    rd = get_profile("successor-dev")
    assert rd is not None
    assert rd.name == "successor-dev"
    assert rd.intro_animation == "successor"


def test_user_profile_overrides_builtin(temp_config_dir: Path) -> None:
    """A user profile with the same name as a builtin wins."""
    user_dir = temp_config_dir / "profiles"
    user_dir.mkdir()
    (user_dir / "default.json").write_text(json.dumps({
        "name": "default",
        "description": "user override",
        "theme": "forge",
    }))

    PROFILE_REGISTRY.reload()
    default = get_profile("default")
    assert default is not None
    assert default.description == "user override"
    assert default.theme == "forge"
    assert PROFILE_REGISTRY.source_of("default") == "user"


def test_user_profile_loads_alongside_builtin(temp_config_dir: Path) -> None:
    """User-only profile names appear alongside the builtins."""
    user_dir = temp_config_dir / "profiles"
    user_dir.mkdir()
    (user_dir / "research.json").write_text(json.dumps({
        "name": "research",
        "description": "deep reading mode",
    }))

    PROFILE_REGISTRY.reload()
    names = PROFILE_REGISTRY.names()
    assert "default" in names
    assert "successor-dev" in names
    assert "research" in names


def test_broken_user_profile_doesnt_block_builtin(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    user_dir = temp_config_dir / "profiles"
    user_dir.mkdir()
    (user_dir / "broken.json").write_text("{ not json")

    PROFILE_REGISTRY.reload()
    assert get_profile("default") is not None
    assert get_profile("broken") is None
    captured = capsys.readouterr()
    assert "broken.json" in captured.err


# ─── get_active_profile / set_active_profile ───


def test_get_active_returns_default_when_unset(temp_config_dir: Path) -> None:
    """No active_profile in config → returns the `default` builtin."""
    PROFILE_REGISTRY.reload()
    profile = get_active_profile()
    assert profile.name == "default"


def test_get_active_reads_config(temp_config_dir: Path) -> None:
    """active_profile in chat.json is honored."""
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "successor-dev",
    }))
    PROFILE_REGISTRY.reload()
    profile = get_active_profile()
    assert profile.name == "successor-dev"


def test_get_active_falls_back_when_name_missing(temp_config_dir: Path) -> None:
    """active_profile points at a non-existent profile → falls back."""
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "this-profile-does-not-exist",
    }))
    PROFILE_REGISTRY.reload()
    profile = get_active_profile()
    assert profile.name == "default"


def test_set_active_persists_to_config(temp_config_dir: Path) -> None:
    """set_active_profile writes active_profile to chat.json."""
    PROFILE_REGISTRY.reload()
    assert set_active_profile("successor-dev") is True

    cfg = load_chat_config()
    assert cfg["active_profile"] == "successor-dev"


def test_set_active_then_get_roundtrip(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    set_active_profile("successor-dev")
    profile = get_active_profile()
    assert profile.name == "successor-dev"


# ─── next_profile ───


def test_next_profile_cycles(temp_config_dir: Path) -> None:
    """next_profile walks the registry and wraps."""
    PROFILE_REGISTRY.reload()
    profiles = all_profiles()
    assert len(profiles) >= 2  # default + successor-dev

    seen = []
    current = profiles[0]
    for _ in range(len(profiles) + 1):
        seen.append(current.name)
        current = next_profile(current)

    # Full cycle wraps back to the start
    assert seen[0] == seen[-1]
    assert set(seen) >= {p.name for p in profiles}


def test_next_profile_with_none_returns_first(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    first = all_profiles()[0]
    assert next_profile(None).name == first.name


def test_next_profile_with_unknown_returns_first(temp_config_dir: Path) -> None:
    """An unregistered profile (e.g. test fixture) reverts to the first."""
    PROFILE_REGISTRY.reload()
    bogus = Profile(name="not-in-registry")
    first = all_profiles()[0]
    assert next_profile(bogus).name == first.name
