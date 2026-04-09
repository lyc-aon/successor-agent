"""Regression coverage for the self-contained E2E playback HTML."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_e2e_driver_module():
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / "e2e_chat_driver.py"
    spec = importlib.util.spec_from_file_location("successor_e2e_chat_driver", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_playback_html_escapes_embedded_script_terminators(tmp_path: Path) -> None:
    driver = _load_e2e_driver_module()
    scenario = driver.Scenario(
        name="playback_escape",
        description="regression",
        prompts=[],
    )
    timeline = [
        {
            "index": 1,
            "turn_index": 1,
            "kind": "settled",
            "frame_index": None,
            "turn_elapsed_s": 0.1,
            "scenario_elapsed_s": 0.1,
            "agent_turn": 1,
            "stream_open": False,
            "running_tools": 0,
            "message_count": 1,
            "plain": "<script src=\"app.js\"></script>\nEOF",
        }
    ]
    trace_events = [
        {
            "t": 0.1,
            "type": "stream_end",
            "assistant_excerpt": "</script><script>alert('x')</script>",
        }
    ]

    driver._write_playback_html(tmp_path, scenario, timeline, trace_events)
    html = (tmp_path / "playback.html").read_text(encoding="utf-8")

    # One closing tag for the JSON payload script, one for the page JS.
    assert html.count("</script>") == 2
    assert "\\u003c/script\\u003e" in html
    assert "alert('x')" in html
