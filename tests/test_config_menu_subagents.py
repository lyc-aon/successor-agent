"""Config menu coverage for the subagents section."""

from __future__ import annotations

import json
from pathlib import Path

from successor.snapshot import config_demo_snapshot, render_grid_to_plain
from successor.wizard.config import SuccessorConfig, _SETTINGS_TREE, _profile_to_json_dict


def _field_idx(name: str) -> int:
    return next(i for i, field in enumerate(_SETTINGS_TREE) if field.name == name)


def test_subagent_section_renders_in_config_menu(temp_config_dir: Path) -> None:
    grid = config_demo_snapshot(
        rows=55,
        cols=140,
        settings_cursor=_field_idx("subagents_enabled"),
    )
    plain = render_grid_to_plain(grid)
    assert "subagents" in plain.lower()
    assert "max model tasks" in plain.lower()
    assert "notify on finish" in plain.lower()


def test_profile_json_round_trip_includes_subagents(temp_config_dir: Path) -> None:
    from successor.profiles import Profile, SubagentConfig, parse_profile_file

    original = Profile(
        name="roundtrip-subagents",
        subagents=SubagentConfig(
            enabled=False,
            max_model_tasks=2,
            notify_on_finish=False,
            timeout_s=123.0,
        ),
    )
    payload = _profile_to_json_dict(original)
    target = temp_config_dir / "profiles" / "roundtrip-subagents.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))

    parsed = parse_profile_file(target)
    assert parsed is not None
    assert parsed.subagents == original.subagents


def test_config_menu_save_writes_subagent_settings(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    profile = menu._current_profile()

    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("subagents_enabled")],
        False,
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("subagents_max_model_tasks")],
        2,
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("subagents_timeout_s")],
        111.0,
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("subagents_notify_on_finish")],
        False,
    )
    menu._save()

    target = temp_config_dir / "profiles" / f"{profile.name}.json"
    payload = json.loads(target.read_text())
    assert payload["subagents"] == {
        "enabled": False,
        "max_model_tasks": 2,
        "notify_on_finish": False,
        "timeout_s": 111.0,
    }
