"""Tests for the provider presets system.

Covers:
  1. Preset integrity (unique IDs, valid types, non-empty models)
  2. Lookup functions (get_preset_by_id, match_preset)
  3. Preset-provider-type consistency
"""

from __future__ import annotations

import pytest

from successor.providers.presets import (
    PROVIDER_PRESETS,
    ProviderPreset,
    get_preset_by_id,
    match_preset,
)
from successor.providers.factory import PROVIDER_REGISTRY


def test_presets_have_unique_ids() -> None:
    ids = [p.id for p in PROVIDER_PRESETS]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


def test_presets_have_valid_provider_types() -> None:
    for p in PROVIDER_PRESETS:
        assert p.provider_type in PROVIDER_REGISTRY, (
            f"preset '{p.id}' has unknown provider_type '{p.provider_type}'"
        )


def test_default_models_not_empty() -> None:
    for p in PROVIDER_PRESETS:
        assert p.default_model, f"preset '{p.id}' has empty default_model"


def test_labels_not_empty() -> None:
    for p in PROVIDER_PRESETS:
        assert p.label, f"preset '{p.id}' has empty label"


def test_hints_not_empty() -> None:
    for p in PROVIDER_PRESETS:
        assert p.hint, f"preset '{p.id}' has empty hint"


def test_preset_is_frozen() -> None:
    p = PROVIDER_PRESETS[0]
    with pytest.raises(AttributeError):
        p.id = "changed"


# ─── get_preset_by_id ───


def test_get_preset_by_id_found() -> None:
    p = get_preset_by_id("zai")
    assert p is not None
    assert p.label == "z.ai"
    assert p.provider_type == "anthropic"
    assert p.default_model == "glm-5.1"


def test_get_preset_by_id_not_found() -> None:
    assert get_preset_by_id("nonexistent") is None


def test_get_preset_by_id_llamacpp() -> None:
    p = get_preset_by_id("llamacpp")
    assert p is not None
    assert p.needs_api_key is False


# ─── match_preset ───


def test_match_preset_openai() -> None:
    p = match_preset("openai_compat", "https://api.openai.com/v1")
    assert p is not None
    assert p.id == "openai"


def test_match_preset_zai() -> None:
    p = match_preset("anthropic", "https://api.z.ai/api/anthropic")
    assert p is not None
    assert p.id == "zai"


def test_match_preset_trailing_slash() -> None:
    p = match_preset("openai_compat", "https://api.openai.com/v1/")
    assert p is not None
    assert p.id == "openai"


def test_match_preset_case_insensitive() -> None:
    p = match_preset("openai_compat", "HTTPS://API.OPENAI.COM/V1")
    assert p is not None
    assert p.id == "openai"


def test_match_preset_no_match() -> None:
    p = match_preset("openai_compat", "https://custom.example.com/v1")
    assert p is None


def test_match_preset_custom_anthropic() -> None:
    p = match_preset("anthropic", "https://custom-proxy.example.com")
    assert p is None


# ─── Expected presets exist ───


def test_all_expected_presets_present() -> None:
    ids = {p.id for p in PROVIDER_PRESETS}
    expected = {"llamacpp", "ollama", "openai", "anthropic", "zai", "openrouter", "generic", "kimi-code"}
    assert ids == expected
