from __future__ import annotations

import json
from pathlib import Path

from successor.verification_hints import build_repo_verification_guidance


def test_build_repo_verification_guidance_detects_python_and_package_contracts(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
version = "0.0.1"

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.1"]

[tool.ruff]
line-length = 100
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-web",
                "scripts": {
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "test": "vitest run",
                },
            },
        ),
        encoding="utf-8",
    )
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)

    guidance = build_repo_verification_guidance(str(nested))

    assert "Repo verification hints" in guidance
    assert "ruff check" in guidance
    assert "pytest -q" in guidance
    assert "npm run lint" in guidance
    assert "npm run typecheck" in guidance
    assert "npm test" in guidance
