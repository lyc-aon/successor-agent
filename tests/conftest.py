"""Pytest fixtures for hermetic Ronin tests.

The key fixture here is `temp_config_dir`. It creates a clean temp
directory and points `RONIN_CONFIG_DIR` at it for the lifetime of one
test, then cleans up. Both `config.py` (chat config) and `loader.py`
(registries) honor that env var, so the entire user-config surface
becomes hermetic with one fixture — no mocking, no monkeypatching of
filesystem APIs, no test-only code paths in production modules.

Tests that exercise the loader pattern (themes, profiles, skills,
tools) drop fixture files into the relevant subdirectory under the
temp config dir and call `Registry.reload()` to pick them up. This is
exactly how a real user would create their own customizations, which
is the point — the tests exercise the same code path as real use.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def temp_config_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create a hermetic ~/.config/ronin equivalent for one test.

    Sets RONIN_CONFIG_DIR to a temp dir for the duration of the test
    so that:
      - config.py:_config_dir() returns the temp dir
      - loader.py:config_dir() returns the temp dir
      - any user theme/profile/skill files are read from the temp dir
      - any saved chat config writes to the temp dir

    Yields the temp dir as a Path. Cleanup happens automatically when
    the test finishes (tempfile.TemporaryDirectory handles it).
    """
    with tempfile.TemporaryDirectory(prefix="ronin-test-") as tmp:
        path = Path(tmp)
        monkeypatch.setenv("RONIN_CONFIG_DIR", str(path))
        yield path
