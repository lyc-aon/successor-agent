"""Provider presets — friendly service names mapped to provider configs.

The provider registry speaks in protocol types (llamacpp, openai_compat,
anthropic) but users think in services (OpenAI, z.ai, Ollama). Presets
bridge that gap: each preset is a named template that fills in the right
protocol type, base URL, default model, and API key requirement.

Used by the setup wizard and config menu so users pick a friendly name
instead of memorizing URLs and protocol types. The underlying profile
JSON still stores raw provider config — presets are a UI convenience.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """One named service template for the provider picker UI."""

    id: str              # Machine key: "zai", "openai", "openrouter", etc.
    label: str           # Display name: "z.ai", "openai", etc.
    hint: str            # One-line help text shown next to the label
    provider_type: str   # Protocol type: "anthropic", "openai_compat", "llamacpp"
    base_url: str        # Default endpoint URL
    default_model: str   # Default model ID for this service
    needs_api_key: bool  # Whether the service requires an API key

    # Optional per-tool configuration defaults keyed by tool name. Shape
    # mirrors `Profile.tool_config`, so the wizard can merge entries for
    # enabled tools straight into the new profile. Use this to declare
    # "if you enable tool X on this service, here are sensible defaults" —
    # e.g. a vision sibling endpoint for a text-only primary. Keep api_key
    # empty; `web/vision.py` falls back to the primary client's key.
    tool_defaults: dict[str, dict[str, Any]] = field(default_factory=dict)


# Ordered tuple — the order here is the order the wizard and config
# menu present to the user. Local/free options first, then cloud
# services, then the generic fallback last.
PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="llamacpp",
        label="local llama.cpp",
        hint="free + private, needs llama-server running",
        provider_type="llamacpp",
        base_url="http://localhost:8080",
        default_model="local",
        needs_api_key=False,
    ),
    ProviderPreset(
        id="ollama",
        label="ollama",
        hint="local models via ollama serve",
        provider_type="openai_compat",
        base_url="http://localhost:11434",
        default_model="llama3",
        needs_api_key=False,
    ),
    ProviderPreset(
        id="openai",
        label="openai",
        hint="api.openai.com — api key required",
        provider_type="openai_compat",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4.1-mini",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="anthropic",
        label="anthropic",
        hint="anthropic.com — api key required",
        provider_type="anthropic",
        base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4-6-20250514",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="zai",
        label="z.ai",
        hint="GLM models via Anthropic-compatible endpoint",
        provider_type="anthropic",
        base_url="https://api.z.ai/api/anthropic",
        default_model="glm-5.1",
        needs_api_key=True,
        # GLM-5.1 is text-only. Pair it with glm-4.6v on the SAME Anthropic-
        # compatible endpoint — that route is covered by the GLM Coding
        # Plan subscription, whereas glm-5v-turbo + /paas/v4 is direct
        # pay-as-you-go and 429s for subscription-only users. The vision
        # tool reuses the primary client's api_key via the fallback in
        # web/vision.py.
        tool_defaults={
            "vision": {
                "mode": "endpoint",
                "provider_type": "anthropic",
                "base_url": "https://api.z.ai/api/anthropic",
                "model": "glm-4.6v",
                "timeout_s": 120.0,
                "max_tokens": 16384,
                "detail": "auto",
            },
        },
    ),
    ProviderPreset(
        id="openrouter",
        label="openrouter",
        hint="free models available, no card needed",
        provider_type="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        default_model="openai/gpt-oss-20b:free",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="generic",
        label="generic openai-compat",
        hint="any /v1/chat/completions endpoint",
        provider_type="openai_compat",
        base_url="http://localhost:1234",
        default_model="local-model",
        needs_api_key=False,
    ),
    ProviderPreset(
        id="kimi-code",
        label="Kimi Code",
        hint="OAuth device flow — run `successor login` first",
        provider_type="openai_compat",
        base_url="https://api.kimi.com/coding/v1",
        default_model="kimi-k2-5",
        needs_api_key=False,
    ),
)


def get_preset_by_id(preset_id: str) -> ProviderPreset | None:
    """Look up a preset by its id field. Returns None if not found."""
    for p in PROVIDER_PRESETS:
        if p.id == preset_id:
            return p
    return None


def match_preset(provider_type: str, base_url: str) -> ProviderPreset | None:
    """Try to match an existing provider config to a preset.

    Used by the config menu to show the right preset label when loading
    an existing profile. Matches on provider_type + base_url (case-insensitive,
    trailing slashes stripped). Returns None if no preset matches — the
    caller should show "custom" in that case.
    """
    normalized_url = base_url.rstrip("/").lower()
    for p in PROVIDER_PRESETS:
        if p.provider_type == provider_type and p.base_url.rstrip("/").lower() == normalized_url:
            return p
    return None
