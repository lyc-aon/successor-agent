"""Resolved configuration for holonet routes, browser control, and vision."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..loader import config_dir


HOLO_DEFAULT_PROVIDER_OPTIONS = (
    "auto",
    "brave_search",
    "brave_news",
    "firecrawl_search",
    "firecrawl_scrape",
    "europe_pmc",
    "clinicaltrials",
    "biomedical_research",
)
VISION_MODE_OPTIONS = ("inherit", "endpoint")
VISION_PROVIDER_OPTIONS = ("llamacpp", "openai_compat")


def _read_secret_file(path: str | None) -> str:
    if not path:
        return ""
    expanded = os.path.expanduser(os.path.expandvars(path))
    try:
        text = Path(expanded).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def _first_nonempty_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class HolonetConfig:
    default_provider: str = "auto"
    brave_enabled: bool = True
    brave_api_key: str = ""
    brave_api_key_file: str = ""
    firecrawl_enabled: bool = True
    firecrawl_api_key: str = ""
    firecrawl_api_key_file: str = ""
    europe_pmc_enabled: bool = True
    clinicaltrials_enabled: bool = True
    biomedical_enabled: bool = True

    def effective_brave_key(self) -> str:
        return (
            self.brave_api_key.strip()
            or _read_secret_file(self.brave_api_key_file)
            or _first_nonempty_env("SUCCESSOR_BRAVE_API_KEY", "BRAVE_API_KEY")
        )

    def effective_firecrawl_key(self) -> str:
        return (
            self.firecrawl_api_key.strip()
            or _read_secret_file(self.firecrawl_api_key_file)
            or _first_nonempty_env("SUCCESSOR_FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY")
        )


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    headless: bool = True
    channel: str = "chrome"
    python_executable: str = ""
    executable_path: str = ""
    user_data_dir: str = ""
    viewport_width: int = 1440
    viewport_height: int = 960
    timeout_s: float = 20.0
    screenshot_on_error: bool = True

    def resolved_user_data_dir(self, profile_name: str = "default") -> Path:
        if self.user_data_dir.strip():
            return Path(os.path.expanduser(os.path.expandvars(self.user_data_dir)))
        safe_name = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "-"
            for ch in profile_name.strip().lower()
        ) or "default"
        return config_dir() / "browser" / safe_name

    def resolved_executable_path(self) -> str:
        return os.path.expanduser(os.path.expandvars(self.executable_path)).strip()

    def resolved_python_executable(self) -> str:
        value = os.path.expanduser(os.path.expandvars(self.python_executable)).strip()
        return value or sys.executable


@dataclass(frozen=True, slots=True)
class VisionConfig:
    mode: str = "inherit"
    provider_type: str = "llamacpp"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_file: str = ""
    timeout_s: float = 120.0
    max_tokens: int = 1024
    detail: str = "auto"

    def effective_api_key(self) -> str:
        return (
            self.api_key.strip()
            or _read_secret_file(self.api_key_file)
            or _first_nonempty_env("SUCCESSOR_VISION_API_KEY", "OPENAI_API_KEY")
        )


def resolve_holonet_config(profile: Any) -> HolonetConfig:
    if profile is None:
        return HolonetConfig()
    tool_config = getattr(profile, "tool_config", None) or {}
    raw = tool_config.get("holonet") or {}
    provider = str(raw.get("default_provider", "auto") or "auto").strip().lower()
    if provider not in HOLO_DEFAULT_PROVIDER_OPTIONS:
        provider = "auto"
    try:
        return HolonetConfig(
            default_provider=provider,
            brave_enabled=bool(raw.get("brave_enabled", True)),
            brave_api_key=str(raw.get("brave_api_key", "") or ""),
            brave_api_key_file=str(raw.get("brave_api_key_file", "") or ""),
            firecrawl_enabled=bool(raw.get("firecrawl_enabled", True)),
            firecrawl_api_key=str(raw.get("firecrawl_api_key", "") or ""),
            firecrawl_api_key_file=str(raw.get("firecrawl_api_key_file", "") or ""),
            europe_pmc_enabled=bool(raw.get("europe_pmc_enabled", True)),
            clinicaltrials_enabled=bool(raw.get("clinicaltrials_enabled", True)),
            biomedical_enabled=bool(raw.get("biomedical_enabled", True)),
        )
    except (TypeError, ValueError):
        return HolonetConfig()


def resolve_browser_config(profile: Any) -> BrowserConfig:
    if profile is None:
        return BrowserConfig()
    tool_config = getattr(profile, "tool_config", None) or {}
    raw = tool_config.get("browser") or {}
    try:
        width = int(raw.get("viewport_width", 1440))
        height = int(raw.get("viewport_height", 960))
        timeout_s = float(raw.get("timeout_s", 20.0))
        width = max(640, min(3840, width))
        height = max(480, min(2160, height))
        timeout_s = max(1.0, min(120.0, timeout_s))
        return BrowserConfig(
            headless=bool(raw.get("headless", True)),
            channel=str(raw.get("channel", "chrome") or "chrome").strip(),
            python_executable=str(raw.get("python_executable", "") or ""),
            executable_path=str(raw.get("executable_path", "") or ""),
            user_data_dir=str(raw.get("user_data_dir", "") or ""),
            viewport_width=width,
            viewport_height=height,
            timeout_s=timeout_s,
            screenshot_on_error=bool(raw.get("screenshot_on_error", True)),
        )
    except (TypeError, ValueError):
        return BrowserConfig()


def resolve_vision_config(profile: Any) -> VisionConfig:
    if profile is None:
        return VisionConfig()
    tool_config = getattr(profile, "tool_config", None) or {}
    raw = tool_config.get("vision") or {}
    try:
        mode = str(raw.get("mode", "inherit") or "inherit").strip().lower()
        if mode not in VISION_MODE_OPTIONS:
            mode = "inherit"
        provider_type = str(raw.get("provider_type", "llamacpp") or "llamacpp").strip().lower()
        if provider_type not in VISION_PROVIDER_OPTIONS:
            provider_type = "llamacpp"
        timeout_s = float(raw.get("timeout_s", 120.0))
        max_tokens = int(raw.get("max_tokens", 1024))
        detail = str(raw.get("detail", "auto") or "auto").strip().lower()
        if detail not in {"auto", "low", "high", "original"}:
            detail = "auto"
        timeout_s = max(5.0, min(600.0, timeout_s))
        max_tokens = max(64, min(8192, max_tokens))
        return VisionConfig(
            mode=mode,
            provider_type=provider_type,
            base_url=str(raw.get("base_url", "") or ""),
            model=str(raw.get("model", "") or ""),
            api_key=str(raw.get("api_key", "") or ""),
            api_key_file=str(raw.get("api_key_file", "") or ""),
            timeout_s=timeout_s,
            max_tokens=max_tokens,
            detail=detail,
        )
    except (TypeError, ValueError):
        return VisionConfig()
