"""Config menu coverage for holonet, browser, and vision sections."""

from __future__ import annotations

import json
import stat
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


def test_vision_section_hidden_until_tool_enabled(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    plain = _paint(menu)
    assert "base url" not in plain.lower()

    cur = menu._current_profile()
    menu._working_profiles[menu._profile_cursor] = replace(cur, tools=("vision",))
    plain = _paint(menu)
    assert "vision" in plain.lower()
    assert "max tokens" in plain.lower()


def test_config_menu_save_writes_holonet_browser_and_vision_settings(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    profile = menu._current_profile()
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("tools")],
        ("holonet", "browser", "vision"),
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
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("vision_mode")],
        "endpoint",
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("vision_provider_type")],
        "openai_compat",
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("vision_base_url")],
        "http://127.0.0.1:8090",
    )
    menu._set_field_on_profile(
        menu._profile_cursor,
        _SETTINGS_TREE[_field_idx("vision_model")],
        "vision-local",
    )
    menu._save()

    target = temp_config_dir / "profiles" / f"{profile.name}.json"
    payload = json.loads(target.read_text())
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert payload["tools"] == ["holonet", "browser", "vision"]
    assert payload["tool_config"]["holonet"]["default_provider"] == "firecrawl_search"
    assert payload["tool_config"]["holonet"]["firecrawl_api_key_file"] == "~/keys/firecrawl.txt"
    assert payload["tool_config"]["browser"]["channel"] == "msedge"
    assert payload["tool_config"]["browser"]["headless"] is False
    assert payload["tool_config"]["vision"]["mode"] == "endpoint"
    assert payload["tool_config"]["vision"]["provider_type"] == "openai_compat"
    assert payload["tool_config"]["vision"]["base_url"] == "http://127.0.0.1:8090"
    assert payload["tool_config"]["vision"]["model"] == "vision-local"


def test_profile_json_round_trip_preserves_tool_config(temp_config_dir: Path) -> None:
    from successor.profiles import Profile, parse_profile_file

    original = Profile(
        name="roundtrip-web-config",
        tools=("holonet", "browser", "vision"),
        tool_config={
            "holonet": {"default_provider": "clinicaltrials"},
            "browser": {"channel": "chrome", "headless": False},
            "vision": {"mode": "endpoint", "base_url": "http://127.0.0.1:8090", "model": "vision-local"},
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
