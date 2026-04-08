"""Config menu coverage for holonet and browser sections."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from successor.snapshot import render_grid_to_plain
from successor.render.cells import Grid
from successor.wizard.config import SuccessorConfig, _SETTINGS_TREE, _profile_to_json_dict


def _field_idx(name: str) -> int:
    return next(i for i, field in enumerate(_SETTINGS_TREE) if field.name == name)


def _paint(menu: SuccessorConfig, *, rows: int = 60, cols: int = 150) -> str:
    grid = Grid(rows, cols)
    menu.on_tick(grid)
    return render_grid_to_plain(grid)


def test_holonet_section_hidden_until_tool_enabled(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    plain = _paint(menu)
    assert "default provider" not in plain.lower()

    cur = menu._current_profile()
    menu._working_profiles[menu._profile_cursor] = replace(cur, tools=("holonet",))
    plain = _paint(menu)
    assert "holonet" in plain.lower()
    assert "default provider" in plain.lower()


def test_browser_section_hidden_until_tool_enabled(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    plain = _paint(menu)
    assert "user data dir" not in plain.lower()

    cur = menu._current_profile()
    menu._working_profiles[menu._profile_cursor] = replace(cur, tools=("browser",))
    plain = _paint(menu)
    assert "browser" in plain.lower()
    assert "user data dir" in plain.lower()


def test_config_menu_save_writes_holonet_and_browser_settings(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    profile = menu._current_profile()
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("tools")],
        ("holonet", "browser"),
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("holonet_default_provider")],
        "firecrawl_search",
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("holonet_firecrawl_api_key_file")],
        "~/keys/firecrawl.txt",
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("browser_channel")],
        "msedge",
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("browser_headless")],
        False,
    )
    menu._save()

    target = temp_config_dir / "profiles" / f"{profile.name}.json"
    payload = json.loads(target.read_text())
    assert payload["tools"] == ["holonet", "browser"]
    assert payload["tool_config"]["holonet"]["default_provider"] == "firecrawl_search"
    assert payload["tool_config"]["holonet"]["firecrawl_api_key_file"] == "~/keys/firecrawl.txt"
    assert payload["tool_config"]["browser"]["channel"] == "msedge"
    assert payload["tool_config"]["browser"]["headless"] is False


def test_profile_json_round_trip_preserves_tool_config(temp_config_dir: Path) -> None:
    from successor.profiles import Profile, parse_profile_file

    original = Profile(
        name="roundtrip-web-config",
        tools=("holonet", "browser"),
        tool_config={
            "holonet": {"default_provider": "clinicaltrials"},
            "browser": {"channel": "chrome", "headless": False},
        },
    )
    payload = _profile_to_json_dict(original)
    target = temp_config_dir / "profiles" / "roundtrip-web-config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))

    parsed = parse_profile_file(target)
    assert parsed is not None
    assert parsed.tools == original.tools
    assert parsed.tool_config == original.tool_config
