"""Smoke + integration tests for the tier-1 polish pass:

  1. The bundled `paper` and `steel` themes load via THEME_REGISTRY
     and parse all 9 oklch / hex color slots in both dark and light
     modes.
  2. A SuccessorChat constructed against either new theme actually
     renders without exploding.
  3. The pyproject.toml description string is accurate (mentions the
     OpenAI-compatible providers, not just llama.cpp).
  4. The CI workflow YAML is valid and references the master branch.

Hermetic via temp_config_dir.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from successor.profiles import Profile
from successor.render.theme import THEME_REGISTRY, find_theme_or_fallback


REPO_ROOT = Path(__file__).resolve().parent.parent


# ─── Themes ───


def test_paper_theme_loads(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    paper = THEME_REGISTRY.get("paper")
    assert paper is not None
    assert paper.name == "paper"
    assert paper.icon  # non-empty
    assert paper.description  # non-empty
    assert THEME_REGISTRY.source_of("paper") == "builtin"


def test_steel_theme_loads(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    steel = THEME_REGISTRY.get("steel")
    assert steel is not None
    assert steel.name == "steel"
    assert steel.icon
    assert steel.description
    assert THEME_REGISTRY.source_of("steel") == "builtin"


def test_paper_theme_has_full_palette(temp_config_dir: Path) -> None:
    """All 9 semantic color slots populated in both dark and light."""
    THEME_REGISTRY.reload()
    paper = THEME_REGISTRY.get("paper")
    assert paper is not None
    for variant_name, variant in (("dark", paper.dark), ("light", paper.light)):
        assert variant.bg is not None, f"paper.{variant_name}.bg missing"
        assert variant.bg_input is not None
        assert variant.bg_footer is not None
        assert variant.fg is not None
        assert variant.fg_dim is not None
        assert variant.fg_subtle is not None
        assert variant.accent is not None
        assert variant.accent_warm is not None
        assert variant.accent_warn is not None


def test_steel_theme_has_full_palette(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    steel = THEME_REGISTRY.get("steel")
    assert steel is not None
    for variant_name, variant in (("dark", steel.dark), ("light", steel.light)):
        assert variant.bg is not None, f"steel.{variant_name}.bg missing"
        assert variant.bg_input is not None
        assert variant.bg_footer is not None
        assert variant.fg is not None
        assert variant.fg_dim is not None
        assert variant.fg_subtle is not None
        assert variant.accent is not None
        assert variant.accent_warm is not None
        assert variant.accent_warn is not None


def test_themes_are_distinct(temp_config_dir: Path) -> None:
    """The two bundled themes should produce different bg colors so
    a user cycling through them sees actual visual variety."""
    THEME_REGISTRY.reload()
    bgs = set()
    for name in ("steel", "paper"):
        theme = THEME_REGISTRY.get(name)
        assert theme is not None, f"missing theme {name}"
        bgs.add(theme.dark.bg)
    assert len(bgs) == 2, "paper and steel should have distinct dark bg colors"


def test_chat_constructs_with_paper_theme(temp_config_dir: Path) -> None:
    """A chat with the paper theme builds end-to-end without exploding."""
    from successor.chat import SuccessorChat

    THEME_REGISTRY.reload()
    profile = Profile(name="paper-test", theme="paper")
    chat = SuccessorChat(profile=profile)
    assert chat.theme.name == "paper"
    # The variant resolution should produce a usable ThemeVariant
    variant = chat._current_variant()
    assert variant is not None


def test_chat_constructs_with_steel_theme(temp_config_dir: Path) -> None:
    """A chat with the steel theme builds end-to-end."""
    from successor.chat import SuccessorChat

    THEME_REGISTRY.reload()
    profile = Profile(name="steel-test", theme="steel")
    chat = SuccessorChat(profile=profile)
    assert chat.theme.name == "steel"
    variant = chat._current_variant()
    assert variant is not None


def test_find_theme_or_fallback_resolves_supported_themes(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    assert find_theme_or_fallback("paper").name == "paper"
    assert find_theme_or_fallback("steel").name == "steel"
    assert find_theme_or_fallback("forge").name == "paper"
    assert find_theme_or_fallback("cobalt").name == "steel"
    # Unknown name still resolves to a fallback (steel by default)
    fallback = find_theme_or_fallback("nonexistent-theme")
    assert fallback.name in ("steel", "paper")


# ─── pyproject description ───


def test_pyproject_description_mentions_openai_compat() -> None:
    """The package description on PyPI / pip show should reflect that
    Successor supports more than just llama.cpp."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    desc_match = re.search(r'^description\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert desc_match is not None, "no description field in pyproject.toml"
    desc = desc_match.group(1)
    # Must mention OpenAI-compatible providers, not just llama.cpp
    assert "OpenAI" in desc, f"description doesn't mention OpenAI: {desc!r}"
    # Must mention the renderer architecture
    assert "renderer" in desc.lower(), f"description doesn't mention renderer: {desc!r}"
    # Must NOT contain the old "for local llama.cpp models" phrasing
    assert "for local llama.cpp models" not in desc


def test_pyproject_description_under_pypi_cap() -> None:
    """PyPI displays descriptions truncated past ~250-300 chars on
    the package summary card. Stay under to avoid truncation."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    desc_match = re.search(r'^description\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert desc_match is not None
    desc = desc_match.group(1)
    assert len(desc) < 300, f"description too long for PyPI summary card: {len(desc)} chars"


# ─── CI workflow ───


def test_ci_workflow_exists() -> None:
    """A workflow file is present so contributors see CI green/red on PRs."""
    workflow = REPO_ROOT / ".github" / "workflows" / "test.yml"
    assert workflow.exists(), "missing .github/workflows/test.yml"


def test_ci_workflow_is_valid_yaml_and_runs_pytest() -> None:
    """Sanity-check the YAML parses and the job actually runs pytest."""
    pytest.importorskip("yaml", reason="PyYAML not installed in dev env")
    import yaml

    workflow_path = REPO_ROOT / ".github" / "workflows" / "test.yml"
    parsed = yaml.safe_load(workflow_path.read_text())

    # Top-level structure
    assert "jobs" in parsed
    # NOTE: PyYAML interprets the bare key `on` as the boolean True, so
    # in the parsed dict the trigger config lives under True instead of
    # the literal string "on". Both forms are valid YAML.
    triggers = parsed.get("on") or parsed.get(True)
    assert triggers is not None, f"no trigger config in workflow: {parsed.keys()}"
    assert "push" in triggers
    assert "pull_request" in triggers

    # The pytest job exists and runs the test command
    jobs = parsed["jobs"]
    assert "pytest" in jobs
    job = jobs["pytest"]
    assert job["runs-on"].startswith("ubuntu")

    # Walk steps and confirm one of them invokes pytest
    steps = job["steps"]
    has_pytest_run = any(
        "pytest" in str(step.get("run", ""))
        for step in steps
    )
    assert has_pytest_run, "no step in the workflow actually runs pytest"


def test_ci_workflow_targets_master_branch() -> None:
    """The workflow should fire on master pushes (the project's
    default branch), not the legacy 'main'."""
    workflow_path = REPO_ROOT / ".github" / "workflows" / "test.yml"
    text = workflow_path.read_text()
    assert "master" in text


# ─── README badge ───


def test_readme_includes_ci_badge() -> None:
    """Visitors landing on the repo should see CI status at a glance."""
    readme = (REPO_ROOT / "README.md").read_text()
    assert "actions/workflows/test.yml/badge.svg" in readme, (
        "README missing the CI status badge"
    )


def test_readme_includes_license_badge() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    assert "license-Apache" in readme or "license-Apache%202.0" in readme


# ─── CONTRIBUTING.md ───


def test_contributing_file_exists_and_mentions_tests() -> None:
    """A bare-minimum sanity check that CONTRIBUTING.md is present
    and tells contributors how to run the test suite."""
    contributing = REPO_ROOT / "CONTRIBUTING.md"
    assert contributing.exists()
    text = contributing.read_text()
    assert "pytest" in text
    assert "974" in text or "tests" in text  # mentions the suite somewhere
