"""Tests for semantic diff rendering on tool cards.

Coverage:
  1. Deterministic change capture for mutating commands
  2. Unified diff parsing for explicit diff commands
  3. Render-path styling for added/removed rows
  4. Async chat finalization path (BashRunner -> settled ToolCard)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from successor.bash import dispatch_bash, paint_tool_card, resolve_bash_config
from successor.bash.prepared_output import PreparedToolOutput
from successor.chat import SuccessorChat
from successor.profiles import Profile
from successor.render.cells import Grid
from successor.render.theme import find_theme_or_fallback
from successor.snapshot import render_grid_to_plain


THEME = find_theme_or_fallback("steel").variant("dark")


def test_dispatch_write_file_captures_added_diff(tmp_path: Path) -> None:
    card = dispatch_bash(
        "cat > note.txt <<'EOF'\nalpha\nbeta\nEOF",
        cwd=str(tmp_path),
    )
    assert card.change_artifact is not None
    file_change = card.change_artifact.files[0]
    assert file_change.path == "note.txt"
    assert file_change.status == "added"
    assert file_change.hunks
    assert "+alpha" in file_change.hunks[0].lines
    assert "+beta" in file_change.hunks[0].lines


def test_dispatch_write_file_captures_modified_diff(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("alpha\nbeta\n")
    card = dispatch_bash(
        "cat > note.txt <<'EOF'\nalpha\ngamma\nEOF",
        cwd=str(tmp_path),
    )
    assert card.change_artifact is not None
    file_change = card.change_artifact.files[0]
    assert file_change.status == "modified"
    lines = file_change.hunks[0].lines
    assert "-beta" in lines
    assert "+gamma" in lines


def test_dispatch_delete_file_captures_removed_diff(tmp_path: Path) -> None:
    (tmp_path / "gone.txt").write_text("alpha\nbeta\n")
    card = dispatch_bash("rm gone.txt", cwd=str(tmp_path))
    assert card.change_artifact is not None
    file_change = card.change_artifact.files[0]
    assert file_change.status == "deleted"
    lines = file_change.hunks[0].lines
    assert "-alpha" in lines
    assert "-beta" in lines


def test_prepared_output_parses_git_diff_as_semantic_rows(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "note.txt").write_text("alpha\nbeta\n")
    subprocess.run(["git", "add", "note.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "note.txt").write_text("alpha\ngamma\n")

    card = dispatch_bash("git diff -- note.txt", cwd=str(tmp_path))
    prep = PreparedToolOutput(card)
    rows = prep.layout(120)
    kinds = [row.kind for row in rows]
    assert "diff_file" in kinds
    assert "diff_hunk" in kinds
    assert "diff_remove" in kinds
    assert "diff_add" in kinds


def test_paint_diff_rows_uses_distinct_added_removed_styles(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("alpha\nbeta\n")
    card = dispatch_bash(
        "cat > note.txt <<'EOF'\nalpha\ngamma\nEOF",
        cwd=str(tmp_path),
    )
    g = Grid(30, 100)
    paint_tool_card(g, card, x=0, y=0, w=100, theme=THEME)
    plain = render_grid_to_plain(g)
    add_row = _row_index_containing(plain, "+gamma")
    remove_row = _row_index_containing(plain, "-beta")
    add_cell = _first_non_space_cell(g, add_row)
    remove_cell = _first_non_space_cell(g, remove_row)
    assert add_cell.style.bg != THEME.bg_input
    assert remove_cell.style.bg != THEME.bg_input
    assert add_cell.style.fg != remove_cell.style.fg


def test_chat_async_runner_finalizes_with_change_artifact(tmp_path: Path) -> None:
    profile = Profile(
        name="diff-test",
        tools=("bash",),
        tool_config={"bash": {
            "allow_dangerous": False,
            "allow_mutating": True,
            "timeout_s": 30.0,
            "max_output_bytes": 8192,
            "working_directory": str(tmp_path),
        }},
    )
    chat = SuccessorChat(profile=profile)
    bash_cfg = resolve_bash_config(chat.profile)
    assert chat._spawn_bash_runner(
        "cat > note.txt <<'EOF'\nalpha\nbeta\nEOF",
        bash_cfg=bash_cfg,
    )
    deadline = time.monotonic() + 3.0
    while chat._running_tools and time.monotonic() < deadline:
        chat._pump_running_tools()
        time.sleep(0.02)
    assert not chat._running_tools
    tool_msg = next(msg for msg in chat.messages if msg.tool_card is not None)
    assert tool_msg.tool_card is not None
    assert tool_msg.tool_card.change_artifact is not None
    assert tool_msg.tool_card.change_artifact.files[0].path == "note.txt"


def _row_index_containing(plain: str, needle: str) -> int:
    for idx, line in enumerate(plain.splitlines()):
        if needle in line:
            return idx
    raise AssertionError(f"missing row containing {needle!r}")


def _first_non_space_cell(grid: Grid, row: int):
    for col in range(grid.cols):
        cell = grid.at(row, col)
        if cell.wide_tail:
            continue
        if cell.char.strip():
            return cell
    raise AssertionError(f"row {row} contains no visible cells")
