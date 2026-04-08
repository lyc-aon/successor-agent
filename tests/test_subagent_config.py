"""Tests for SubagentConfig and profile JSON integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from successor.profiles import Profile, SubagentConfig, parse_profile_file


def test_default_subagent_config_is_valid() -> None:
    cfg = SubagentConfig()
    assert cfg.enabled is True
    assert cfg.max_model_tasks == 1
    assert cfg.notify_on_finish is True
    assert cfg.timeout_s > 0


def test_subagent_config_rejects_zero_max_model_tasks() -> None:
    with pytest.raises(ValueError, match="max_model_tasks"):
        SubagentConfig(max_model_tasks=0)


def test_subagent_config_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        SubagentConfig(timeout_s=0.0)


def test_subagent_config_round_trip() -> None:
    original = SubagentConfig(
        enabled=False,
        max_model_tasks=3,
        notify_on_finish=False,
        timeout_s=120.5,
    )
    rebuilt = SubagentConfig.from_dict(original.to_dict())
    assert rebuilt == original


def test_subagent_config_from_dict_lenient_fallbacks() -> None:
    cfg = SubagentConfig.from_dict({
        "enabled": True,
        "max_model_tasks": -5,
        "notify_on_finish": False,
        "timeout_s": "slow",
    })
    assert cfg.enabled is True
    assert cfg.notify_on_finish is False
    assert cfg.max_model_tasks == SubagentConfig().max_model_tasks
    assert cfg.timeout_s == SubagentConfig().timeout_s


def test_profile_default_has_subagent_config() -> None:
    profile = Profile(name="test")
    assert profile.subagents == SubagentConfig()


def test_parse_profile_subagents_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    target.write_text(json.dumps({
        "name": "worker",
        "subagents": {
            "enabled": False,
            "max_model_tasks": 2,
            "notify_on_finish": False,
            "timeout_s": 45.0,
        },
    }))
    profile = parse_profile_file(target)
    assert profile is not None
    assert profile.subagents.enabled is False
    assert profile.subagents.max_model_tasks == 2
    assert profile.subagents.notify_on_finish is False
    assert profile.subagents.timeout_s == 45.0
