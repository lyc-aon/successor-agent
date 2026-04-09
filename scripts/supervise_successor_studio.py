from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from successor.chat import SuccessorChat, _Message
from successor.config import save_chat_config
from successor.playback import write_playback_html
from successor.profiles import Profile
from successor.providers import make_provider

from e2e_chat_driver import (
    ARTIFACT_ROOT,
    AssertionResult,
    GRID_COLS,
    GRID_ROWS,
    Scenario,
    TurnStats,
    _load_trace_events,
    build_profile,
    dump_snapshots,
    run_user_prompt,
    write_index,
)


SERVER_PORT = 8765
REFERENCE_FILES = (
    Path("/home/lycaon/dev/skills/skills/design-system.md"),
    Path("/home/lycaon/dev/skills/skills/frontend-patterns.md"),
    Path("/home/lycaon/dev/skills/skills/visual-iteration-loop.md"),
    Path("/home/lycaon/dev/skills/skills/web-dev-critique.md"),
)


def _studio_root() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return ARTIFACT_ROOT / "successor_studio_supervised" / stamp


def _start_server(workspace: Path, log_dir: Path) -> subprocess.Popen[str]:
    stdout_path = log_dir / "http-server.out"
    stderr_path = log_dir / "http-server.err"
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(SERVER_PORT), "--bind", "127.0.0.1"],
        cwd=str(workspace),
        stdout=stdout_path.open("w", encoding="utf-8"),
        stderr=stderr_path.open("w", encoding="utf-8"),
        text=True,
    )


def _capture_browser_screenshot(url: str, image_path: Path, console_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover
        console_path.write_text(
            json.dumps([{"type": "capture_error", "text": str(exc)}], indent=2),
            encoding="utf-8",
        )
        return False

    console_entries: list[dict[str, str]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": 1440, "height": 1000},
                color_scheme="light",
            )
            page.on(
                "console",
                lambda msg: console_entries.append({"type": msg.type, "text": msg.text}),
            )
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.screenshot(path=str(image_path), full_page=True)
            browser.close()
    except Exception as exc:  # pragma: no cover
        console_entries.append({"type": "capture_error", "text": str(exc)})
        console_path.write_text(json.dumps(console_entries, indent=2), encoding="utf-8")
        return False

    console_path.write_text(json.dumps(console_entries, indent=2), encoding="utf-8")
    return True


def _make_profile(workspace: Path, base_url: str, model: str) -> Profile:
    profile = build_profile(workspace, base_url, model)
    browser_cfg = {
        "headless": True,
        "channel": "chrome",
        "python_executable": "/usr/bin/python3",
        "timeout_s": 20.0,
        "viewport_width": 1440,
        "viewport_height": 960,
    }
    return Profile(
        name="studio-supervised",
        description="Interactive supervised Successor Studio build",
        theme=profile.theme,
        display_mode=profile.display_mode,
        density=profile.density,
        system_prompt=(
            "You are successor — a focused, brief assistant building and iterating on "
            "a local single-page app called Successor Studio. Use bash for file creation "
            "and edits. Use the browser for live verification and interaction. This should "
            "feel like a serious internal product, not a toy demo. Favor a strong shell, "
            "clear information hierarchy, polished panels, and thoughtful spacing. "
            "If a listed skill clearly matches the request, load it before using the browser. "
            "For inspect or polish prompts, sample one or two representative interactions, "
            "fix the most important issue, verify it, and stop. Do not pad with bookkeeping-only turns. "
            "When the prompt says ready, do one quick browser sanity check before you say it is ready. "
            "Answer in one short paragraph after each prompt."
        ),
        provider=profile.provider,
        skills=("browser-verifier", "browser-operator"),
        tools=("bash", "browser"),
        tool_config={
            "bash": dict((profile.tool_config or {}).get("bash") or {}),
            "browser": browser_cfg,
        },
        intro_animation=None,
    )


def _seed_context(chat: SuccessorChat, app_url: str) -> None:
    refs = "\n".join(f"- {path}" for path in REFERENCE_FILES)
    chat.messages = [
        _Message("user", f"Local app URL for browser checks: {app_url}"),
        _Message(
            "user",
            "Local design references are available if useful. Consult them selectively with bash, "
            "do not cargo-cult them:\n" + refs,
        ),
    ]


def _finalize_bundle(
    out_dir: Path,
    scenario: Scenario,
    all_stats: list[TurnStats],
    trace_events: list[dict[str, object]],
    timeline: list[dict[str, object]],
) -> None:
    (out_dir / "timeline.json").write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    (out_dir / "session_trace.json").write_text(json.dumps(trace_events, indent=2), encoding="utf-8")
    write_playback_html(
        out_dir,
        title=f"Successor E2E Playback - {scenario.name}",
        description=scenario.description,
        frames=timeline,
        trace_events=trace_events,
    )
    write_index(
        out_dir,
        scenario,
        all_stats,
        assertions=[],
        trace_events=trace_events,
    )
    (out_dir / "assertions.json").write_text(
        json.dumps([asdict(AssertionResult(name="recorded", passed=True, detail="interactive supervised run"))], indent=2),
        encoding="utf-8",
    )


def main() -> int:
    root = _studio_root()
    workspace = root / "workspace"
    log_dir = root / "logs"
    visuals_dir = root / "visuals"
    config_dir = root / "config"
    workspace.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    visuals_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    old_config_dir = os.environ.get("SUCCESSOR_CONFIG_DIR")
    os.environ["SUCCESSOR_CONFIG_DIR"] = str(config_dir)
    save_chat_config({"autorecord": False})

    server = _start_server(workspace, log_dir)
    time.sleep(0.5)

    scenario = Scenario(
        name="successor_studio_supervised",
        description="Interactive supervised build of Successor Studio",
        prompts=[],
    )
    timeline: list[dict[str, object]] = []
    scenario_t0 = time.monotonic()
    all_stats: list[TurnStats] = []

    app_url = f"http://127.0.0.1:{SERVER_PORT}/index.html"
    profile = _make_profile(workspace, base_url="http://localhost:8080", model="qwopus")

    chat = SuccessorChat()
    chat.profile = profile
    chat.system_prompt = profile.system_prompt
    chat.client = make_provider(profile.provider)
    chat.messages = []
    _seed_context(chat, app_url)

    print(
        json.dumps(
            {
                "run_dir": str(root),
                "workspace": str(workspace),
                "app_url": app_url,
                "grid": {"rows": GRID_ROWS, "cols": GRID_COLS},
            }
        ),
        flush=True,
    )

    turn_index = 1
    try:
        for line in sys.stdin:
            prompt = line.rstrip("\n")
            if not prompt:
                continue
            if prompt == "__EXIT__":
                break

            try:
                stats = run_user_prompt(
                    chat,
                    prompt,
                    turn_index,
                    root,
                    True,
                    frame_interval_s=0.15,
                    timeline=timeline,
                    scenario_t0=scenario_t0,
                )
            except Exception as exc:  # pragma: no cover
                stats = TurnStats(
                    turn_index=turn_index,
                    user_prompt=prompt,
                    agent_turns_consumed=0,
                    tool_cards_appended=0,
                    tool_cards_executed=0,
                    tool_cards_refused=0,
                    wall_clock_s=0.0,
                    settled_cleanly=False,
                    notes=[f"crashed: {type(exc).__name__}: {exc}"],
                )
                (root / f"turn_{turn_index:02d}_crash.txt").write_text(
                    traceback.format_exc(),
                    encoding="utf-8",
                )

            dump_snapshots(
                chat,
                workspace,
                root,
                stats,
                timeline=timeline,
                scenario_t0=scenario_t0,
            )
            all_stats.append(stats)

            image_path = visuals_dir / f"turn_{turn_index:02d}_app.png"
            console_path = visuals_dir / f"turn_{turn_index:02d}_console.json"
            shot_ok = False
            if (workspace / "index.html").exists():
                shot_ok = _capture_browser_screenshot(app_url, image_path, console_path)

            payload = {
                "turn": turn_index,
                "prompt": prompt,
                "stats": asdict(stats),
                "screenshot": str(image_path) if shot_ok else None,
                "console": str(console_path) if shot_ok else None,
                "latest_plain": str(root / f"turn_{turn_index:02d}_plain.txt"),
                "latest_messages": str(root / f"turn_{turn_index:02d}_messages.json"),
            }
            (root / f"turn_{turn_index:02d}_summary.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(payload), flush=True)
            turn_index += 1
    finally:
        try:
            if hasattr(chat, "_shutdown_runtime_for_exit"):
                chat._shutdown_runtime_for_exit()
        except Exception:
            pass
        trace_events = _load_trace_events(config_dir / "logs")
        _finalize_bundle(root, scenario, all_stats, trace_events, timeline)
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        if old_config_dir is None:
            os.environ.pop("SUCCESSOR_CONFIG_DIR", None)
        else:
            os.environ["SUCCESSOR_CONFIG_DIR"] = old_config_dir

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
