"""Workspace-aware verification hints for the agent loop.

These helpers inspect the current working tree just enough to surface
deterministic validation guidance in the system prompt. The goal is not
to invent checks, but to point the model at the repo's real contract
when the contract is discoverable from local config files.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import tomllib


def build_repo_verification_guidance(working_directory: str) -> str:
    """Return concrete validation hints for the current workspace."""
    root = _find_workspace_root(Path(working_directory))
    hints: list[str] = [
        "Before reporting completion, verify the changed behavior with commands or runtime checks rather than source inspection alone.",
        "If a relevant verification step could not be run, say that plainly instead of implying success.",
    ]

    pyproject_hints = _python_hints(root)
    package_hints = _package_hints(root)
    hints.extend(pyproject_hints)
    hints.extend(package_hints)

    unique_hints: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        cleaned = " ".join(hint.split()).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique_hints.append(f"- {cleaned}")

    if not unique_hints:
        return ""
    return "## Repo verification hints\n\n" + "\n".join(unique_hints)


def _find_workspace_root(start: Path) -> Path:
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
        if (candidate / "pyproject.toml").exists():
            return candidate
        if (candidate / "package.json").exists():
            return candidate
    return current


def _python_hints(root: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []
    data = _load_toml(pyproject)
    if not isinstance(data, dict):
        return []

    hints: list[str] = []
    if _ruff_is_configured(data):
        hints.append(
            "This workspace advertises Ruff. After touching Python files, run `ruff check` on the touched paths or `ruff check src tests` before finishing.",
        )
    if _pytest_is_configured(data, root):
        hints.append(
            "This workspace advertises pytest. Run targeted tests for behavior changes and use `pytest -q` before closing broad refactors when practical.",
        )
    return hints


def _package_hints(root: Path) -> list[str]:
    package_json = root / "package.json"
    if not package_json.exists():
        return []
    data = _load_json(package_json)
    if not isinstance(data, dict):
        return []
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []

    hints: list[str] = []
    if isinstance(scripts.get("lint"), str):
        hints.append(
            "This workspace has `npm run lint`. Use it when you touch frontend, browser, or build-facing code.",
        )
    if isinstance(scripts.get("typecheck"), str):
        hints.append(
            "This workspace has `npm run typecheck`. Run it when TypeScript or shared typed frontend code changes.",
        )
    if isinstance(scripts.get("test"), str):
        hints.append(
            "This workspace has `npm test`. Run the relevant JS/TS tests when web behavior changes.",
        )
    return hints


def _ruff_is_configured(pyproject: dict[str, Any]) -> bool:
    tool = pyproject.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("ruff"), dict):
        return True

    project = pyproject.get("project")
    if not isinstance(project, dict):
        return False
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict):
        return False
    for group in optional.values():
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str) and item.strip().lower().startswith("ruff"):
                return True
    return False


def _pytest_is_configured(pyproject: dict[str, Any], root: Path) -> bool:
    tool = pyproject.get("tool")
    if isinstance(tool, dict):
        pytest_tool = tool.get("pytest")
        if isinstance(pytest_tool, dict):
            return True
    project = pyproject.get("project")
    if isinstance(project, dict):
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if not isinstance(group, list):
                    continue
                for item in group:
                    if isinstance(item, str) and item.strip().lower().startswith("pytest"):
                        return True
    if (root / "pytest.ini").is_file():
        return True
    if (root / "tests").is_dir():
        return True
    return False


@lru_cache(maxsize=32)
def _load_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data if isinstance(data, dict) else None


@lru_cache(maxsize=32)
def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
