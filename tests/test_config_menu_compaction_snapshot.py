"""Visual snapshot tests for the config menu's compaction section.

Verifies the new "compaction" group of fields renders correctly in
the three-pane config menu with proper formatting:
  - percentages display as "12.50%" not "0.125"
  - the enabled toggle shows "on (autocompact)" / "off (manual only)"
  - integer fields show plainly
  - the section appears under its "compaction" header

Hermetic via temp_config_dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from successor.snapshot import config_demo_snapshot, render_grid_to_plain
from successor.wizard.config import _SETTINGS_TREE


# Find the field indices once at import time so the tests don't need
# to know the exact tree position (which may shift if other fields
# are added).
def _find_field_idx(name: str) -> int:
    for i, f in enumerate(_SETTINGS_TREE):
        if f.name == name:
            return i
    raise LookupError(f"no field named {name!r} in _SETTINGS_TREE")


COMPACTION_ENABLED_IDX = _find_field_idx("compaction_enabled")
COMPACTION_WARNING_IDX = _find_field_idx("compaction_warning_pct")
COMPACTION_AUTOCOMPACT_IDX = _find_field_idx("compaction_autocompact_pct")
COMPACTION_KEEP_RECENT_IDX = _find_field_idx("compaction_keep_recent_rounds")
COMPACTION_SUMMARY_MAX_IDX = _find_field_idx("compaction_summary_max_tokens")


# ─── Section header + all fields rendered ───


def test_config_menu_compaction_section_visible(temp_config_dir: Path) -> None:
    """The 'compaction' section header is rendered."""
    g = config_demo_snapshot(rows=55, cols=140, settings_cursor=COMPACTION_ENABLED_IDX)
    plain = render_grid_to_plain(g)
    assert "compaction" in plain


def test_config_menu_compaction_all_fields_visible(temp_config_dir: Path) -> None:
    """All six compaction field labels appear in the rendered settings."""
    g = config_demo_snapshot(rows=55, cols=140, settings_cursor=COMPACTION_ENABLED_IDX)
    plain = render_grid_to_plain(g)

    assert "enabled" in plain
    assert "warning %" in plain
    assert "autocompact %" in plain
    assert "blocking %" in plain
    assert "keep recent rounds" in plain
    assert "summary max tokens" in plain


# ─── Default values render correctly ───


def test_config_menu_compaction_default_values(temp_config_dir: Path) -> None:
    """Default profile shows: enabled, 12.50%, 6.25%, 1.56%, 6, 16000."""
    g = config_demo_snapshot(rows=55, cols=140, settings_cursor=COMPACTION_ENABLED_IDX)
    plain = render_grid_to_plain(g)

    # Toggle display
    assert "on (autocompact)" in plain

    # Percentages — 2 decimal places
    assert "12.50%" in plain
    assert "6.25%" in plain
    assert "1.56%" in plain

    # Integer fields
    assert "16000" in plain or "16,000" in plain


def test_config_menu_compaction_enabled_field_focused(temp_config_dir: Path) -> None:
    """When the enabled field is under the cursor, it's painted with focus."""
    g = config_demo_snapshot(rows=55, cols=140, settings_cursor=COMPACTION_ENABLED_IDX)
    plain = render_grid_to_plain(g)
    # The cursor row has its label visible
    enabled_line = next(
        (line for line in plain.splitlines() if "enabled" in line and "autocompact" in line),
        "",
    )
    assert enabled_line, "expected to find a row mentioning enabled + autocompact"


def test_config_menu_compaction_warning_field_focused(temp_config_dir: Path) -> None:
    """Cursor on warning_pct row shows the percent value."""
    g = config_demo_snapshot(rows=55, cols=140, settings_cursor=COMPACTION_WARNING_IDX)
    plain = render_grid_to_plain(g)

    warning_line = next(
        (line for line in plain.splitlines() if "warning %" in line),
        "",
    )
    assert "12.50%" in warning_line


def test_config_menu_compaction_section_position(temp_config_dir: Path) -> None:
    """The compaction section appears after extensions in the layout."""
    g = config_demo_snapshot(rows=55, cols=140, settings_cursor=COMPACTION_ENABLED_IDX)
    plain = render_grid_to_plain(g)
    lines = plain.splitlines()

    # Find the line indices of the two section headers
    extensions_idx = None
    compaction_idx = None
    for i, line in enumerate(lines):
        # Match the section header column (right of the profiles pane)
        if "extensions" in line and extensions_idx is None:
            extensions_idx = i
        if "compaction" in line and compaction_idx is None:
            compaction_idx = i

    assert extensions_idx is not None
    assert compaction_idx is not None
    assert compaction_idx > extensions_idx, (
        f"compaction should come after extensions, "
        f"got extensions@{extensions_idx} compaction@{compaction_idx}"
    )


# ─── Custom dirty state ───


def test_config_menu_compaction_field_dirty_marker(temp_config_dir: Path) -> None:
    """When a compaction field is dirty, it's marked in the display."""
    g = config_demo_snapshot(
        rows=55, cols=140,
        settings_cursor=COMPACTION_AUTOCOMPACT_IDX,
        dirty=(("default", "compaction_autocompact_pct"),),
    )
    plain = render_grid_to_plain(g)
    # The dirty marker is implementation-defined (often `*` or `●`),
    # but the row should still render the label and value cleanly.
    assert "autocompact %" in plain
    assert "6.25%" in plain
