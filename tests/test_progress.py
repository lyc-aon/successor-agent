from __future__ import annotations

from pathlib import Path

from successor.bash.cards import ToolCard
from successor.bash.diff_artifact import ChangeArtifact, ChangedFile
from successor.progress import (
    combine_progress_updates,
    summarize_subagent_completion,
    summarize_tool_completion,
)
from successor.subagents.manager import SubagentTaskSnapshot


def test_browser_inspect_summary_is_high_signal() -> None:
    card = ToolCard(
        verb="inspect-page",
        tool_name="browser",
        tool_arguments={"action": "inspect"},
        exit_code=0,
    )

    update = summarize_tool_completion(
        card,
        metadata={
            "controls_summary": "Visible controls:\n- button: \"Save\"; selector=#save\n- textbox: \"Search\"; selector=#search",
        },
    )

    assert update is not None
    assert update.important is True
    assert "2 visible controls" in update.text


def test_single_low_signal_progress_update_is_suppressed() -> None:
    card = ToolCard(
        verb="read-file",
        tool_name="bash",
        params=(("path", "README.md"),),
        exit_code=0,
    )

    update = summarize_tool_completion(card)
    assert update is not None
    assert combine_progress_updates([update]) is None


def test_multiple_low_signal_updates_combine() -> None:
    updates = [
        summarize_tool_completion(
            ToolCard(
                verb="read-file",
                tool_name="bash",
                params=(("path", "README.md"),),
                exit_code=0,
            )
        ),
        summarize_tool_completion(
            ToolCard(
                verb="search-files",
                tool_name="bash",
                params=(("pattern", "browser verifier"),),
                exit_code=0,
            )
        ),
    ]

    summary = combine_progress_updates([item for item in updates if item is not None])
    assert summary is not None
    assert summary.startswith("progress: ")
    assert "read file README.md" in summary


def test_changed_file_summary_is_high_signal() -> None:
    card = ToolCard(
        verb="write-file",
        tool_name="bash",
        exit_code=0,
        change_artifact=ChangeArtifact(
            files=(ChangedFile(path="src/app.js", status="modified"),),
        ),
    )

    update = summarize_tool_completion(card)
    assert update is not None
    assert update.important is True
    assert "updated src/app.js" == update.text


def test_failed_changed_file_summary_mentions_partial_failure() -> None:
    card = ToolCard(
        verb="write-file",
        tool_name="bash",
        exit_code=2,
        change_artifact=ChangeArtifact(
            files=(ChangedFile(path="index.html", status="modified"),),
        ),
    )

    update = summarize_tool_completion(card)
    assert update is not None
    assert update.important is True
    assert "failed after touching index.html" in update.text


def test_subagent_completion_summary_uses_excerpt() -> None:
    snapshot = SubagentTaskSnapshot(
        task_id="t001",
        name="version audit",
        directive="check version",
        status="completed",
        created_at=0.0,
        started_at=0.1,
        finished_at=0.2,
        transcript_path=Path("/tmp/t001.json"),
        result_excerpt="found version 0.1.21",
        result_text="found version 0.1.21",
    )

    update = summarize_subagent_completion(snapshot)
    assert update is not None
    assert update.important is True
    assert "subagent version audit finished: found version 0.1.21" == update.text
