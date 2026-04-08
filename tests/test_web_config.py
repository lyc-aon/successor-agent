"""Config resolution coverage for holonet and browser tools."""

from __future__ import annotations

from pathlib import Path

from successor.profiles import Profile
from successor.web import resolve_browser_config, resolve_holonet_config


def test_resolve_holonet_config_reads_keys_and_files(
    temp_config_dir: Path,
    tmp_path: Path,
) -> None:
    secret = tmp_path / "firecrawl.key"
    secret.write_text("fc-secret\n", encoding="utf-8")
    profile = Profile(
        name="holonet-config",
        tool_config={
            "holonet": {
                "default_provider": "firecrawl_search",
                "firecrawl_api_key_file": str(secret),
                "brave_enabled": False,
            }
        },
    )

    cfg = resolve_holonet_config(profile)
    assert cfg.default_provider == "firecrawl_search"
    assert cfg.brave_enabled is False
    assert cfg.effective_firecrawl_key() == "fc-secret"


def test_resolve_browser_config_uses_defaults(temp_config_dir: Path) -> None:
    profile = Profile(name="browser-defaults")
    cfg = resolve_browser_config(profile)

    assert cfg.headless is True
    assert cfg.channel == "chrome"
    assert cfg.viewport_width == 1440
    assert cfg.viewport_height == 960
    assert cfg.timeout_s == 20.0
    assert cfg.resolved_user_data_dir("browser-defaults").name == "browser-defaults"


def test_resolve_browser_config_clamps_values(temp_config_dir: Path) -> None:
    profile = Profile(
        name="browser-tuning",
        tool_config={
            "browser": {
                "viewport_width": 99999,
                "viewport_height": 10,
                "timeout_s": 0.1,
                "headless": False,
                "channel": "msedge",
            }
        },
    )

    cfg = resolve_browser_config(profile)
    assert cfg.headless is False
    assert cfg.channel == "msedge"
    assert cfg.viewport_width == 3840
    assert cfg.viewport_height == 480
    assert cfg.timeout_s == 1.0
