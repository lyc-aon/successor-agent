"""Config resolution coverage for holonet, browser, and vision tools."""

from __future__ import annotations

from pathlib import Path

from successor.profiles import Profile
from successor.web import (
    resolve_browser_config,
    resolve_holonet_config,
    resolve_vision_config,
)


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


def test_resolve_vision_config_reads_keys_and_files(
    temp_config_dir: Path,
    tmp_path: Path,
) -> None:
    secret = tmp_path / "vision.key"
    secret.write_text("vision-secret\n", encoding="utf-8")
    profile = Profile(
        name="vision-config",
        tool_config={
            "vision": {
                "mode": "endpoint",
                "provider_type": "openai_compat",
                "base_url": "http://127.0.0.1:8090",
                "model": "vision-local",
                "api_key_file": str(secret),
                "timeout_s": 90.0,
                "max_tokens": 2048,
                "detail": "high",
            }
        },
    )

    cfg = resolve_vision_config(profile)
    assert cfg.mode == "endpoint"
    assert cfg.provider_type == "openai_compat"
    assert cfg.base_url == "http://127.0.0.1:8090"
    assert cfg.model == "vision-local"
    assert cfg.effective_api_key() == "vision-secret"
    assert cfg.timeout_s == 90.0
    assert cfg.max_tokens == 2048
    assert cfg.detail == "high"


def test_resolve_vision_config_uses_defaults_and_clamps(temp_config_dir: Path) -> None:
    profile = Profile(
        name="vision-defaults",
        tool_config={
            "vision": {
                "mode": "bogus",
                "provider_type": "bogus",
                "timeout_s": 9999,
                "max_tokens": 3,
                "detail": "bogus",
            }
        },
    )

    cfg = resolve_vision_config(profile)
    assert cfg.mode == "inherit"
    assert cfg.provider_type == "llamacpp"
    assert cfg.timeout_s == 600.0
    assert cfg.max_tokens == 64
    assert cfg.detail == "auto"
