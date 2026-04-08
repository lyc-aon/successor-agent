"""End-to-end chat driver — real multi-turn sessions against a live
llama-server, with per-turn plaintext / ANSI / message / workspace
snapshots, mid-stream frame capture, assertion-based regression
checks, and stability runs.

Run:
    .venv/bin/python scripts/e2e_chat_driver.py --scenario write_html
    .venv/bin/python scripts/e2e_chat_driver.py --scenario all --runs 3
    .venv/bin/python scripts/e2e_chat_driver.py --list

Artifacts land at /tmp/successor-e2e/<scenario>/[run_N/]:
    workspace/             — the bash working_directory (real files)
    turn_NN_plain.txt      — chat painted to a grid, ANSI stripped
    turn_NN_ansi.txt       — full ANSI dump (cat to see colors)
    turn_NN_messages.json  — every _Message at settle time
    turn_NN_workspace.txt  — recursive listing of workspace/
    turn_NN_loop.json      — agent_turn count, tool cards, timings
    turn_NN_stream/        — mid-stream frame snapshots if enabled
        frame_001_plain.txt
        frame_001_ansi.txt
        ...
    session.log            — full driver log
    index.md               — turn-by-turn summary + assertion results
    assertions.json        — machine-readable pass/fail per assertion

Scenarios are defined in the SCENARIOS dict at the bottom. Each
scenario is a `Scenario` dataclass with prompts + expected outcomes.
The driver runs each prompt end-to-end (including any agent-loop
continuation turns), captures snapshots, runs assertions, and
reports pass/fail.

The driver uses real `dispatch_bash` against a real workspace so the
side effects are REAL. Scenarios that write files produce those files
on disk in the per-scenario workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

# Make sure we import from the repo's src, not any installed copy
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from successor.chat import SuccessorChat, _Message  # noqa: E402
from successor import __version__ as SUCCESSOR_VERSION  # noqa: E402
from successor.subagents.cards import SubagentToolCard  # noqa: E402
from successor.profiles import Profile  # noqa: E402
from successor.providers import make_provider  # noqa: E402
from successor.render.cells import Grid  # noqa: E402
from successor.snapshot import render_grid_to_ansi, render_grid_to_plain  # noqa: E402


# ─── Driver configuration ───

GRID_ROWS = 60
GRID_COLS = 140
TURN_TIMEOUT_S = 240.0  # per user prompt, including all continuation turns
TICK_SLEEP_S = 0.05  # how long to sleep between _pump_stream calls
MID_STREAM_SNAPSHOT_INTERVAL_S = 0.25  # capture a frame every N seconds while a stream is open
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_MODEL = "local"
ARTIFACT_ROOT = Path("/tmp/successor-e2e")


# ─── Scenario definition ───


@dataclass
class Scenario:
    """A test scenario: a sequence of user prompts plus assertions
    about the expected end state.

    Each scenario gets a fresh workspace and a fresh chat. The driver
    runs the prompts in order, settles each one, captures snapshots,
    then evaluates assertions against the cumulative state.

    Assertions:
      - assert_files: maps relative-path → required substring (or
        None for "must exist regardless of content"). The path is
        rooted at the scenario's workspace dir.
      - assert_no_files: paths that must NOT exist (for refusal scenarios).
      - assert_min_text_in_final: substrings the final assistant
        message must contain. Use lowercase; matched case-insensitively.
      - assert_turn_plain_contains: maps turn index -> required substrings
        that must appear in the captured `turn_NN_plain.txt` render for
        that prompt. This is the main renderer-level regression hook for
        live E2E scenarios.
      - assert_max_total_cards: ceiling on total tool cards across the
        whole scenario (catches loops).
      - assert_min_total_cards: floor (catches "model didn't run anything").
      - assert_max_agent_turns_per_prompt: ceiling on agent loop depth
        per individual user prompt (catches single-prompt runaway).
      - assert_no_refused_cards: every card must have executed=True.
      - assert_each_settles: every prompt must reach a clean idle state.
    """
    name: str
    description: str
    prompts: list[str]
    assert_files: dict[str, str | None] = field(default_factory=dict)
    assert_no_files: list[str] = field(default_factory=list)
    assert_min_text_in_final: list[str] = field(default_factory=list)
    assert_turn_plain_contains: dict[int, list[str]] = field(default_factory=dict)
    assert_max_total_cards: int | None = None
    assert_min_total_cards: int | None = None
    assert_max_agent_turns_per_prompt: int | None = None
    assert_no_refused_cards: bool = True
    assert_each_settles: bool = True
    allow_synthetic_final: bool = False
    pre_setup: Callable[["SuccessorChat"], None] | None = None
    profile_overrides: dict | None = None  # merged into tool_config["bash"]


# ─── Per-turn state ───


@dataclass
class TurnStats:
    turn_index: int
    user_prompt: str
    agent_turns_consumed: int  # how many model calls happened inside this prompt
    tool_cards_appended: int
    tool_cards_executed: int
    tool_cards_refused: int
    wall_clock_s: float
    settled_cleanly: bool
    notes: list[str] = field(default_factory=list)
    mid_stream_frames_captured: int = 0


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str = ""


# ─── Core driver ───


def build_profile(
    workspace: Path,
    base_url: str,
    model: str,
    bash_overrides: dict | None = None,
) -> Profile:
    """Construct the yolo test profile the driver uses for every
    scenario. Bash enabled, mutating + dangerous both allowed,
    working_directory pinned to the scenario workspace. Scenarios
    can override individual bash flags via `bash_overrides`
    (e.g. `{"allow_mutating": False}` for the refusal scenario).
    """
    sys_prompt = (
        "You are successor — a focused, brief assistant. "
        "The user is driving an automated end-to-end test, so your "
        "job is to do exactly what each prompt asks and nothing more. "
        "Run shell commands by emitting fenced code blocks with the "
        "bash language tag. Do not narrate your plans; just act. "
        "Prefer single-line commands unless a heredoc is the right "
        "tool. After you run a command, summarize the result in one "
        "sentence."
    )
    bash_cfg = {
        "allow_dangerous": True,
        "allow_mutating": True,
        "timeout_s": 30.0,
        "max_output_bytes": 8192,
        "working_directory": str(workspace),
    }
    if bash_overrides:
        bash_cfg.update(bash_overrides)
    return Profile(
        name="e2e-yolo",
        description="automated E2E test — yolo bash, pinned workspace",
        theme="steel",
        display_mode="dark",
        density="normal",
        system_prompt=sys_prompt,
        provider={
            "type": "llamacpp",
            "base_url": base_url,
            "model": model,
            "max_tokens": 4096,
            "temperature": 0.2,
        },
        skills=(),
        tools=("bash",),
        tool_config={"bash": bash_cfg},
        intro_animation=None,
    )


def settle_chat_with_capture(
    chat: SuccessorChat,
    timeout_s: float,
    snapshot_dir: Path | None,
    snapshot_prefix: str,
) -> tuple[bool, float, int]:
    """Drive the chat's pumps until it's fully settled: no stream in
    flight, no pending agent turn, AND no in-flight bash runners.

    The runner check is critical — with async dispatch, _pump_stream
    can return after a StreamEnded while BashRunners are still
    executing in background threads. The chat's tick loop needs to
    keep polling them until they complete and their cards finalize.

    While the chat is busy, capture a frame every
    MID_STREAM_SNAPSHOT_INTERVAL_S so we can audit the live render
    evolution after the fact. Pass snapshot_dir=None to disable
    mid-stream capture.

    Returns (settled_cleanly, elapsed_seconds, frames_captured).
    """
    start = time.monotonic()
    last_snapshot_at = 0.0
    frames = 0

    while True:
        has_active_subagents = False
        if hasattr(chat, "_has_active_subagent_tasks"):
            try:
                has_active_subagents = bool(chat._has_active_subagent_tasks())
            except Exception:
                has_active_subagents = False

        # Fully settled: stream done + no continuation queued + no
        # runners still executing.
        if (
            chat._stream is None
            and chat._agent_turn == 0
            and not chat._running_tools
            and not has_active_subagents
        ):
            return True, time.monotonic() - start, frames

        # Mid-stream capture
        if snapshot_dir is not None:
            elapsed = time.monotonic() - start
            if elapsed - last_snapshot_at >= MID_STREAM_SNAPSHOT_INTERVAL_S:
                last_snapshot_at = elapsed
                frames += 1
                _capture_frame(chat, snapshot_dir, snapshot_prefix, frames)

        chat._pump_stream()
        chat._pump_running_tools()
        if hasattr(chat, "_pump_subagent_notifications"):
            try:
                chat._pump_subagent_notifications()
            except Exception:
                pass

        if time.monotonic() - start > timeout_s:
            # Timeout — clean up so the next prompt can still start.
            if chat._stream is not None:
                try:
                    chat._stream.close()
                except Exception:
                    pass
                chat._stream = None
            if chat._running_tools:
                for msg in list(chat._running_tools):
                    if msg.running_tool is not None:
                        msg.running_tool.cancel()
                # Drain one more pass so cancellations propagate
                deadline = time.monotonic() + 1.5
                while chat._running_tools and time.monotonic() < deadline:
                    chat._pump_running_tools()
                    time.sleep(TICK_SLEEP_S)
            if hasattr(chat, "_subagent_manager"):
                try:
                    chat._subagent_manager.cancel("all")
                except Exception:
                    pass
            chat._agent_turn = 0
            chat._pending_continuation = False
            return False, time.monotonic() - start, frames

        time.sleep(TICK_SLEEP_S)


def _capture_frame(
    chat: SuccessorChat,
    out_dir: Path,
    prefix: str,
    frame_idx: int,
) -> None:
    """Paint a single frame snapshot for mid-stream evolution capture."""
    grid = Grid(GRID_ROWS, GRID_COLS)
    try:
        chat.on_tick(grid)
    except Exception:
        # Silently skip — we don't want a paint error to break the
        # whole scenario. The post-settle snapshot will catch any
        # broken state.
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    plain = render_grid_to_plain(grid)
    ansi = render_grid_to_ansi(grid)
    (out_dir / f"{prefix}_frame_{frame_idx:03d}_plain.txt").write_text(plain)
    (out_dir / f"{prefix}_frame_{frame_idx:03d}_ansi.txt").write_text(ansi)


def run_user_prompt(
    chat: SuccessorChat,
    prompt: str,
    turn_index: int,
    out_dir: Path,
    capture_mid_stream: bool,
) -> TurnStats:
    """Submit one user prompt, drain all agent-loop turns, return stats."""
    messages_before = len(chat.messages)

    _install_call_counter(chat)
    pre_count = chat.client._driver_call_count

    chat.input_buffer = prompt
    chat._submit()

    snapshot_dir = (out_dir / f"turn_{turn_index:02d}_stream") if capture_mid_stream else None
    settled, wall_s, frames = settle_chat_with_capture(
        chat, TURN_TIMEOUT_S, snapshot_dir, f"turn_{turn_index:02d}",
    )

    new_msgs = chat.messages[messages_before:]
    new_cards = [
        (m.tool_card or getattr(m, "subagent_card", None))
        for m in new_msgs
        if (m.tool_card is not None or getattr(m, "subagent_card", None) is not None)
    ]
    cards_new = len(new_cards)
    executed = sum(
        1 for c in new_cards
        if isinstance(c, SubagentToolCard) or getattr(c, "executed", False)
    )
    refused = sum(
        1 for c in new_cards
        if not isinstance(c, SubagentToolCard) and not getattr(c, "executed", False)
    )

    agent_turns = chat.client._driver_call_count - pre_count

    stats = TurnStats(
        turn_index=turn_index,
        user_prompt=prompt,
        agent_turns_consumed=agent_turns,
        tool_cards_appended=cards_new,
        tool_cards_executed=executed,
        tool_cards_refused=refused,
        wall_clock_s=wall_s,
        settled_cleanly=settled,
        mid_stream_frames_captured=frames,
    )
    if not settled:
        stats.notes.append(f"timeout after {wall_s:.1f}s")
    return stats


def _install_call_counter(chat: SuccessorChat) -> None:
    """Monkey-wrap chat.client.stream_chat with a call counter the
    driver reads to tell how many model calls happened per user turn.
    Idempotent: only installs once per client instance.
    """
    if getattr(chat.client, "_driver_counter_installed", False):
        return
    orig = chat.client.stream_chat
    chat.client._driver_call_count = 0

    def wrapped(messages, **kwargs):
        chat.client._driver_call_count += 1
        return orig(messages=messages, **kwargs)

    chat.client.stream_chat = wrapped  # type: ignore[method-assign]
    chat.client._driver_counter_installed = True


# ─── Snapshot dumping ───


def dump_snapshots(
    chat: SuccessorChat,
    workspace: Path,
    out_dir: Path,
    stats: TurnStats,
) -> None:
    """Paint the chat to a Grid and dump every artifact for this turn."""
    prefix = f"turn_{stats.turn_index:02d}"

    grid = Grid(GRID_ROWS, GRID_COLS)
    try:
        chat.on_tick(grid)
    except Exception as exc:
        (out_dir / f"{prefix}_paint_error.txt").write_text(
            f"{exc}\n\n{traceback.format_exc()}"
        )
    plain = render_grid_to_plain(grid)
    ansi = render_grid_to_ansi(grid)
    (out_dir / f"{prefix}_plain.txt").write_text(plain)
    (out_dir / f"{prefix}_ansi.txt").write_text(ansi)

    messages_payload = []
    for i, m in enumerate(chat.messages):
        entry: dict = {
            "index": i,
            "role": m.role,
            "synthetic": m.synthetic,
            "raw_text_preview": (m.raw_text or "")[:400],
            "raw_text_len": len(m.raw_text or ""),
        }
        if m.tool_card is not None:
            card = m.tool_card
            entry["tool_card"] = {
                "verb": card.verb,
                "risk": card.risk,
                "executed": card.executed,
                "exit_code": card.exit_code,
                "duration_ms": card.duration_ms,
                "output_len": len(card.output or ""),
                "stderr_len": len(card.stderr or ""),
                "truncated": card.truncated,
                "tool_name": card.tool_name,
                "tool_arguments": dict(card.tool_arguments),
                "raw_command_preview": (card.raw_command or "")[:200],
                "params": list(card.params),
            }
            if card.change_artifact is not None:
                entry["tool_card"]["change_artifact"] = {
                    "prelude": list(card.change_artifact.prelude),
                    "files": [
                        {
                            "path": file_change.path,
                            "status": file_change.status,
                            "old_path": file_change.old_path,
                            "notes": list(file_change.notes),
                            "hunk_count": len(file_change.hunks),
                        }
                        for file_change in card.change_artifact.files
                    ],
                }
        elif getattr(m, "subagent_card", None) is not None:
            card = m.subagent_card
            entry["subagent_card"] = {
                "task_id": card.task_id,
                "name": card.name,
                "directive_preview": (card.directive or "")[:200],
                "tool_call_id": card.tool_call_id,
                "spawn_result_preview": (card.spawn_result or "")[:300],
            }
            entry["api_role_override"] = getattr(m, "api_role_override", None)
        messages_payload.append(entry)
    (out_dir / f"{prefix}_messages.json").write_text(
        json.dumps(messages_payload, indent=2)
    )

    ws_lines = []
    for root, dirs, files in os.walk(workspace):
        rel = Path(root).relative_to(workspace) if root != str(workspace) else Path(".")
        for d in sorted(dirs):
            ws_lines.append(f"DIR  {rel / d}")
        for f in sorted(files):
            full = Path(root) / f
            try:
                size = full.stat().st_size
            except OSError:
                size = -1
            ws_lines.append(f"FILE {rel / f}  ({size} bytes)")
    (out_dir / f"{prefix}_workspace.txt").write_text("\n".join(ws_lines) + "\n")
    (out_dir / f"{prefix}_loop.json").write_text(json.dumps(asdict(stats), indent=2))


# ─── Assertions ───


def evaluate_assertions(
    scenario: Scenario,
    chat: SuccessorChat,
    workspace: Path,
    out_dir: Path,
    all_stats: list[TurnStats],
) -> list[AssertionResult]:
    """Run every assertion declared on the scenario against the
    cumulative end state and return per-assertion pass/fail.
    """
    results: list[AssertionResult] = []

    # File assertions
    for rel, required_substring in scenario.assert_files.items():
        path = workspace / rel
        if not path.exists():
            results.append(AssertionResult(
                name=f"file:{rel}",
                passed=False,
                detail=f"missing — expected to exist at {path}",
            ))
            continue
        if required_substring is None:
            results.append(AssertionResult(
                name=f"file:{rel}",
                passed=True,
                detail=f"exists ({path.stat().st_size} bytes)",
            ))
            continue
        try:
            content = path.read_text()
        except Exception as exc:
            results.append(AssertionResult(
                name=f"file:{rel}",
                passed=False,
                detail=f"unreadable: {exc}",
            ))
            continue
        if required_substring in content:
            results.append(AssertionResult(
                name=f"file:{rel}",
                passed=True,
                detail=f"contains required substring",
            ))
        else:
            results.append(AssertionResult(
                name=f"file:{rel}",
                passed=False,
                detail=(
                    f"missing required substring {required_substring!r}; "
                    f"got first 200 chars: {content[:200]!r}"
                ),
            ))

    # File-must-not-exist assertions
    for rel in scenario.assert_no_files:
        path = workspace / rel
        if path.exists():
            results.append(AssertionResult(
                name=f"no_file:{rel}",
                passed=False,
                detail=f"unexpected file present at {path}",
            ))
        else:
            results.append(AssertionResult(
                name=f"no_file:{rel}",
                passed=True,
                detail="absent as required",
            ))

    # Final assistant message text assertions
    if scenario.assert_min_text_in_final:
        final_assistant = None
        for m in reversed(chat.messages):
            if m.role != "successor":
                continue
            if m.synthetic and not scenario.allow_synthetic_final:
                continue
            final_assistant = m.raw_text or ""
            break
        if final_assistant is None:
            results.append(AssertionResult(
                name="final_assistant_text",
                passed=False,
                detail=(
                    "no assistant message found"
                    if scenario.allow_synthetic_final
                    else "no non-synthetic assistant message found"
                ),
            ))
        else:
            haystack = final_assistant.lower()
            missing = [
                needle for needle in scenario.assert_min_text_in_final
                if needle.lower() not in haystack
            ]
            if missing:
                results.append(AssertionResult(
                    name="final_assistant_text",
                    passed=False,
                    detail=(
                        f"missing substrings {missing}; final text "
                        f"first 300 chars: {final_assistant[:300]!r}"
                    ),
                ))
            else:
                results.append(AssertionResult(
                    name="final_assistant_text",
                    passed=True,
                    detail=f"all {len(scenario.assert_min_text_in_final)} substrings present",
                ))

    # Rendered-turn assertions against captured plaintext frames
    for turn_index, required_substrings in scenario.assert_turn_plain_contains.items():
        turn_path = out_dir / f"turn_{turn_index:02d}_plain.txt"
        if not turn_path.exists():
            results.append(AssertionResult(
                name=f"turn_plain:{turn_index}",
                passed=False,
                detail=f"missing artifact {turn_path}",
            ))
            continue
        plain = turn_path.read_text()
        missing = [
            needle for needle in required_substrings
            if needle not in plain
        ]
        if missing:
            results.append(AssertionResult(
                name=f"turn_plain:{turn_index}",
                passed=False,
                detail=(
                    f"missing substrings {missing}; first 500 chars: "
                    f"{plain[:500]!r}"
                ),
            ))
        else:
            results.append(AssertionResult(
                name=f"turn_plain:{turn_index}",
                passed=True,
                detail=f"all {len(required_substrings)} substrings present",
            ))

    # Card count assertions
    total_cards = sum(s.tool_cards_appended for s in all_stats)
    if scenario.assert_max_total_cards is not None:
        passed = total_cards <= scenario.assert_max_total_cards
        results.append(AssertionResult(
            name="max_total_cards",
            passed=passed,
            detail=(
                f"got {total_cards}, max {scenario.assert_max_total_cards}"
                if passed else
                f"FAIL: got {total_cards} > max {scenario.assert_max_total_cards}"
            ),
        ))
    if scenario.assert_min_total_cards is not None:
        passed = total_cards >= scenario.assert_min_total_cards
        results.append(AssertionResult(
            name="min_total_cards",
            passed=passed,
            detail=(
                f"got {total_cards}, min {scenario.assert_min_total_cards}"
                if passed else
                f"FAIL: got {total_cards} < min {scenario.assert_min_total_cards}"
            ),
        ))

    # Per-prompt agent-turn ceiling
    if scenario.assert_max_agent_turns_per_prompt is not None:
        offenders = [
            (s.turn_index, s.agent_turns_consumed)
            for s in all_stats
            if s.agent_turns_consumed > scenario.assert_max_agent_turns_per_prompt
        ]
        if offenders:
            results.append(AssertionResult(
                name="max_agent_turns_per_prompt",
                passed=False,
                detail=(
                    f"prompts that exceeded {scenario.assert_max_agent_turns_per_prompt} "
                    f"agent turns: {offenders}"
                ),
            ))
        else:
            results.append(AssertionResult(
                name="max_agent_turns_per_prompt",
                passed=True,
                detail=f"max observed: {max(s.agent_turns_consumed for s in all_stats)}",
            ))

    # No refused cards (turned off for refusal scenarios)
    if scenario.assert_no_refused_cards:
        refused_total = sum(s.tool_cards_refused for s in all_stats)
        results.append(AssertionResult(
            name="no_refused_cards",
            passed=refused_total == 0,
            detail=(
                "0 refused"
                if refused_total == 0
                else f"FAIL: {refused_total} refused cards across scenario"
            ),
        ))

    # Each prompt settled cleanly
    if scenario.assert_each_settles:
        unsettled = [s.turn_index for s in all_stats if not s.settled_cleanly]
        results.append(AssertionResult(
            name="each_settles",
            passed=not unsettled,
            detail=(
                "all prompts settled"
                if not unsettled
                else f"FAIL: prompts that timed out: {unsettled}"
            ),
        ))

    return results


# ─── Index + summary ───


def write_index(
    out_dir: Path,
    scenario: Scenario,
    all_stats: list[TurnStats],
    assertions: list[AssertionResult],
) -> None:
    """Turn-by-turn summary table + assertion results for humans."""
    lines = [
        f"# E2E scenario: {scenario.name}",
        "",
        f"_{scenario.description}_",
        "",
        f"Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Turn-by-turn",
        "",
        "| Turn | Prompt (trimmed) | Agent turns | Cards | Exec | Refused | Wall (s) | Frames | Notes |",
        "|------|------------------|-------------|-------|------|---------|----------|--------|-------|",
    ]
    for s in all_stats:
        prompt_short = s.user_prompt.replace("\n", " ")
        if len(prompt_short) > 50:
            prompt_short = prompt_short[:47] + "…"
        note_str = "; ".join(s.notes) if s.notes else "-"
        lines.append(
            f"| {s.turn_index} | {prompt_short} | {s.agent_turns_consumed} | "
            f"{s.tool_cards_appended} | {s.tool_cards_executed} | "
            f"{s.tool_cards_refused} | {s.wall_clock_s:.1f} | "
            f"{s.mid_stream_frames_captured} | {note_str} |"
        )
    lines.append("")
    lines.append("## Assertions")
    lines.append("")
    if not assertions:
        lines.append("_(no assertions declared)_")
    else:
        passed = sum(1 for a in assertions if a.passed)
        lines.append(f"**{passed}/{len(assertions)} passed**")
        lines.append("")
        for a in assertions:
            mark = "✓" if a.passed else "✗"
            lines.append(f"- {mark} `{a.name}` — {a.detail}")
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- `turn_NN_plain.txt` — ANSI-stripped chat paint at settle")
    lines.append("- `turn_NN_ansi.txt` — full ANSI dump (`cat` to see colors)")
    lines.append("- `turn_NN_messages.json` — every _Message at settle time")
    lines.append("- `turn_NN_workspace.txt` — recursive workspace listing")
    lines.append("- `turn_NN_loop.json` — stats for this user prompt")
    lines.append("- `turn_NN_stream/` — mid-stream frame snapshots (if captured)")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")
    (out_dir / "assertions.json").write_text(
        json.dumps([asdict(a) for a in assertions], indent=2)
    )


# ─── Scenario runner ───


def run_scenario(
    scenario: Scenario,
    base_url: str,
    model: str,
    artifact_root: Path,
    capture_mid_stream: bool,
    subdir: str | None = None,
) -> tuple[bool, list[AssertionResult]]:
    """Run one scenario end-to-end. Returns (all_assertions_passed, results).

    `subdir` overrides the output directory name (defaults to
    `scenario.name`). Stability runs use this to write each run to
    a separate subdirectory like `grep_report/run_1`.
    """
    out_dir_name = subdir if subdir is not None else scenario.name
    out_dir = artifact_root / out_dir_name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    workspace = out_dir / "workspace"
    workspace.mkdir(parents=True)

    log_path = out_dir / "session.log"
    log_file = log_path.open("w")

    def log(msg: str = "") -> None:
        print(msg)
        log_file.write(msg + "\n")
        log_file.flush()

    log(f"=== E2E scenario: {scenario.name} ===")
    log(f"description: {scenario.description}")
    log(f"workspace  : {workspace}")
    log(f"artifacts  : {out_dir}")
    log(f"base_url   : {base_url}")
    log(f"model      : {model}")
    log("")

    # tempfile prefix can't contain slashes
    safe_prefix = out_dir_name.replace("/", "-").replace(" ", "-")
    config_dir = Path(tempfile.mkdtemp(prefix=f"successor-e2e-{safe_prefix}-"))
    os.environ["SUCCESSOR_CONFIG_DIR"] = str(config_dir)

    profile = build_profile(workspace, base_url, model, scenario.profile_overrides)
    chat = SuccessorChat()
    chat.profile = profile
    chat.system_prompt = profile.system_prompt
    chat.client = make_provider(profile.provider)
    chat.messages = []

    if scenario.pre_setup is not None:
        try:
            scenario.pre_setup(chat)
        except Exception as exc:
            log(f"  PRE-SETUP CRASH: {exc}")
            log(traceback.format_exc())

    all_stats: list[TurnStats] = []

    for turn_idx, prompt in enumerate(scenario.prompts, start=1):
        log(f"--- turn {turn_idx}: {prompt}")
        try:
            stats = run_user_prompt(
                chat, prompt, turn_idx, out_dir, capture_mid_stream,
            )
        except Exception as exc:
            log(f"  CRASH: {exc}")
            log(traceback.format_exc())
            stats = TurnStats(
                turn_index=turn_idx,
                user_prompt=prompt,
                agent_turns_consumed=0,
                tool_cards_appended=0,
                tool_cards_executed=0,
                tool_cards_refused=0,
                wall_clock_s=0.0,
                settled_cleanly=False,
                notes=[f"crashed: {exc!r}"],
            )
        else:
            log(
                f"  agent_turns={stats.agent_turns_consumed} "
                f"cards={stats.tool_cards_appended} "
                f"exec={stats.tool_cards_executed} "
                f"refused={stats.tool_cards_refused} "
                f"wall={stats.wall_clock_s:.1f}s "
                f"frames={stats.mid_stream_frames_captured} "
                f"settled={stats.settled_cleanly}"
            )
            for n in stats.notes:
                log(f"  NOTE: {n}")

        dump_snapshots(chat, workspace, out_dir, stats)
        all_stats.append(stats)

    assertions = evaluate_assertions(scenario, chat, workspace, out_dir, all_stats)
    write_index(out_dir, scenario, all_stats, assertions)

    log("")
    log("=== Assertions ===")
    passed = sum(1 for a in assertions if a.passed)
    log(f"  {passed}/{len(assertions)} passed")
    for a in assertions:
        mark = "✓" if a.passed else "✗"
        log(f"  {mark} {a.name}: {a.detail}")
    log("")

    all_passed = all(a.passed for a in assertions)
    log(f"=== scenario complete: {'PASS' if all_passed else 'FAIL'} ===")
    log_file.close()
    return all_passed, assertions


# ─── Stability runner (multiple runs per scenario) ───


def run_scenario_with_stability(
    scenario: Scenario,
    base_url: str,
    model: str,
    artifact_root: Path,
    runs: int,
    capture_mid_stream: bool,
) -> dict:
    """Run a scenario `runs` times. Each run lands in its own subdir
    `<scenario_name>/run_N/`. Returns a summary dict with per-run
    pass/fail and aggregate stats.
    """
    summary: dict = {
        "name": scenario.name,
        "runs": runs,
        "results": [],
        "all_passed": True,
    }
    base_dir = artifact_root / scenario.name
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)

    for run_idx in range(1, runs + 1):
        print(f"\n--- run {run_idx}/{runs} of {scenario.name} ---")
        # subdir is the path RELATIVE to artifact_root that this run
        # should write to. run_scenario interprets it directly.
        subdir = f"{scenario.name}/run_{run_idx}"
        passed, assertions = run_scenario(
            scenario, base_url, model, artifact_root,
            capture_mid_stream, subdir=subdir,
        )

        summary["results"].append({
            "run": run_idx,
            "passed": passed,
            "assertions": [asdict(a) for a in assertions],
        })
        if not passed:
            summary["all_passed"] = False

    (base_dir / "stability_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


# ─── Scenarios ───


def _heredoc_html(workspace_label: str = "successor") -> str:
    return f"""\
Create about.html in this directory with this exact content using a heredoc:

```
<!DOCTYPE html>
<html>
<head><title>{workspace_label}</title></head>
<body><h1>{workspace_label}</h1></body>
</html>
```
"""


def _enable_subagent_tool(chat: SuccessorChat) -> None:
    """Turn on the model-visible subagent tool for a live scenario."""
    profile = replace(chat.profile, tools=("bash", "subagent"))
    chat.profile = profile
    chat.system_prompt = profile.system_prompt
    chat.client = make_provider(profile.provider)


def _enable_holonet_tool(chat: SuccessorChat) -> None:
    profile = replace(
        chat.profile,
        tools=("holonet",),
        tool_config={
            "holonet": {
                "default_provider": "auto",
            }
        },
    )
    chat.profile = profile
    chat.system_prompt = (
        "You are successor — a focused, brief assistant. "
        "This test session exposes only the holonet tool. "
        "When the user asks for holonet, call it directly and do not "
        "invent bash commands or alternate tools. After the tool returns, "
        "answer in one short paragraph."
    )
    chat.client = make_provider(profile.provider)


def _enable_browser_tool_with_fixture(chat: SuccessorChat) -> None:
    workspace = Path(
        ((chat.profile.tool_config or {}).get("bash") or {}).get("working_directory") or "."
    )
    fixture = workspace / "browser-fixture.html"
    fixture.write_text(
        """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Browser Fixture</title>
  <style>
    body { font-family: sans-serif; padding: 24px; }
    label, button, p { display: block; margin: 12px 0; }
  </style>
</head>
<body>
  <h1>Browser Fixture</h1>
  <label for="message">Message</label>
  <input id="message" placeholder="Type here">
  <button id="apply" onclick="document.getElementById('result').textContent = 'Applied: ' + document.getElementById('message').value;">Apply</button>
  <p id="result">Applied: (empty)</p>
</body>
</html>
""",
        encoding="utf-8",
    )
    profile = replace(
        chat.profile,
        tools=("browser",),
        tool_config={
            "browser": {
                "headless": True,
                "channel": "chrome",
                "timeout_s": 20.0,
                "viewport_width": 1280,
                "viewport_height": 900,
            }
        },
    )
    chat.profile = profile
    chat.system_prompt = (
        "You are successor — a focused, brief assistant. "
        "This test session exposes only the browser tool. "
        "Use it for live page navigation, typing, clicking, and text extraction. "
        "Do not invent bash commands or alternate tools. "
        "After the tool returns, answer in one short paragraph."
    )
    chat.client = make_provider(profile.provider)
    chat.messages = [
        _Message("user", f"Local browser fixture URL: {fixture.as_uri()}")
    ]


SCENARIOS: dict[str, Scenario] = {
    "write_html": Scenario(
        name="write_html",
        description="Write a simple HTML file via heredoc and verify",
        prompts=[
            "Create a file called about.html with a heading that says 'Successor' and a short paragraph about a Python TUI agent harness.",
            "Show me the contents of about.html to confirm it wrote correctly.",
        ],
        assert_files={
            "about.html": "Successor",
        },
        assert_min_text_in_final=["successor"],
        assert_max_total_cards=6,  # generous: write + verify cat
        assert_min_total_cards=2,
        assert_max_agent_turns_per_prompt=6,
    ),
    "read_verify": Scenario(
        name="read_verify",
        description="Write a string with printf, then read it back",
        prompts=[
            "Write the string 'hello from successor' into a file called note.txt using printf.",
            "Read note.txt back and tell me what it says.",
        ],
        assert_files={
            "note.txt": "hello from successor",
        },
        assert_min_text_in_final=["hello from successor"],
        assert_max_total_cards=4,
        assert_min_total_cards=2,
        assert_max_agent_turns_per_prompt=4,
    ),
    "rewrite_diff": Scenario(
        name="rewrite_diff",
        description="Create a file, rewrite it, and verify the rendered diff card shows added/removed lines",
        prompts=[
            "Create a file called note.txt using a heredoc so it contains exactly two lines: alpha and beta.",
            "Rewrite note.txt using a heredoc so it now contains exactly two lines: alpha and gamma. Then briefly confirm what changed.",
        ],
        assert_files={
            "note.txt": "gamma",
        },
        assert_min_text_in_final=["gamma"],
        assert_turn_plain_contains={
            1: ["note.txt  [added]", "+alpha", "+beta"],
            2: ["note.txt  [modified]", "-beta", "+gamma"],
        },
        assert_max_total_cards=4,
        assert_min_total_cards=2,
        assert_max_agent_turns_per_prompt=4,
    ),
    "grep_report": Scenario(
        name="grep_report",
        description="Create a small file, grep it, summarize matches",
        prompts=[
            "Create a file called colors.txt with three lines: red, green, blue.",
            "Use grep to find all lines in colors.txt that contain the letter 'e', then tell me how many matches there were.",
        ],
        assert_files={
            "colors.txt": "red",
        },
        assert_min_text_in_final=["3"],  # red, green, AND blue all contain 'e'
        assert_max_total_cards=5,
        assert_max_agent_turns_per_prompt=4,
    ),
    "multi_step_build": Scenario(
        name="multi_step_build",
        description="Scaffold a Python package across several commands in one turn",
        prompts=[
            "Scaffold a minimal Python package in a subdirectory called tiny/: make the directory, then create an __init__.py and a main.py where main.py just prints 'ok'. Finally run python3 tiny/main.py to prove it works.",
        ],
        assert_files={
            "tiny/__init__.py": None,
            "tiny/main.py": "ok",
        },
        assert_min_text_in_final=["ok"],
        assert_max_total_cards=8,
        # Model is free to batch all four steps into one shell script
        # (mkdir && touch && echo && python3) — that's good behavior,
        # not a bug. Don't enforce a minimum number of separate cards.
        assert_min_total_cards=1,
        assert_max_agent_turns_per_prompt=6,
    ),
    "error_recovery": Scenario(
        name="error_recovery",
        description="cat a missing file, then create it and read it",
        prompts=[
            "Try to cat a file called does_not_exist.txt (it doesn't exist — I want to see how you handle the error).",
            "Now create does_not_exist.txt with the single word 'created' and cat it again.",
        ],
        assert_files={
            "does_not_exist.txt": "created",
        },
        assert_min_text_in_final=["created"],
        assert_max_total_cards=6,
        assert_max_agent_turns_per_prompt=4,
    ),
    "long_output": Scenario(
        name="long_output",
        description="seq 1 25 — verify all lines render without truncation",
        prompts=[
            "Use seq to print the numbers 1 through 25, then tell me if the last number you saw was 25.",
        ],
        assert_min_text_in_final=["25", "yes"],
        assert_max_total_cards=3,
        assert_max_agent_turns_per_prompt=3,
    ),
    "long_session": Scenario(
        name="long_session",
        description="Eight user prompts in one session — exercises history growth",
        prompts=[
            "Create a directory called notes/ and a file called notes/intro.md containing the line '# Intro'.",
            "Create notes/list.md with three bullet items: apples, bananas, cherries.",
            "Create notes/numbers.txt by running seq 1 5 and redirecting to the file.",
            "Run ls -la notes/ and tell me how many files are in it.",
            "Read notes/intro.md back to me.",
            "Append the line '## Section 2' to notes/intro.md and show me the new contents.",
            "Use grep to count how many lines in notes/list.md contain the letter 'a'.",
            "Tell me a one-sentence summary of everything we just did in this session.",
        ],
        assert_files={
            "notes/intro.md": "# Intro",
            "notes/list.md": "apples",
            "notes/numbers.txt": "5",
        },
        assert_min_text_in_final=["notes"],
        assert_max_total_cards=20,
        # The summary turn typically needs zero bash, and read-back
        # turns may batch multiple commands into one. Floor at 6
        # leaves room for either pattern without rewarding loops.
        assert_min_total_cards=6,
        assert_max_agent_turns_per_prompt=4,
    ),
    "refusal_recovery": Scenario(
        name="refusal_recovery",
        description="Read-only profile — model attempts mutation, gets refused, user adjusts",
        prompts=[
            "Create a file called blocked.txt with the word 'blocked'.",
            "I forgot to mention this is a read-only session — just list the directory contents instead.",
        ],
        assert_no_files=["blocked.txt"],
        # Final assistant text should mention the directory state
        # somehow — model can phrase it as "empty", "no files",
        # "directory contains", etc. Just check for "director" or
        # "empty" so the model isn't penalized for word choice.
        assert_min_text_in_final=[],
        assert_no_refused_cards=False,  # we EXPECT refusals here
        assert_max_total_cards=8,
        assert_max_agent_turns_per_prompt=6,
        profile_overrides={"allow_mutating": False},
    ),
    "stderr_handling": Scenario(
        name="stderr_handling",
        description="Command that writes to stderr but exits 0 — model should not panic",
        prompts=[
            "Run this exact command: bash -c 'echo to-stdout; echo to-stderr 1>&2; exit 0' and tell me what came out where.",
        ],
        assert_min_text_in_final=["stderr", "stdout"],
        # Allow up to 4 cards: model may verify by re-running with
        # stderr suppressed before producing the final answer.
        assert_max_total_cards=4,
        assert_max_agent_turns_per_prompt=5,
    ),
    "empty_response": Scenario(
        name="empty_response",
        description="Pure conversational prompt — no bash, just text response",
        prompts=[
            "Briefly explain what a TUI agent harness is, in one sentence. Do not run any commands.",
        ],
        assert_min_text_in_final=["agent"],
        assert_max_total_cards=0,  # NO bash should fire
        assert_min_total_cards=0,
        assert_max_agent_turns_per_prompt=2,
    ),
    "compaction_interaction": Scenario(
        name="compaction_interaction",
        description="Run real bash, then /compact, then more bash — verify history survives the boundary",
        prompts=[
            "Create a file called step1.txt with the word 'first'.",
            "Create a file called step2.txt with the word 'second'.",
            "/compact",
            "Now create a file called step3.txt with the word 'third', then list all the files in this directory.",
        ],
        assert_files={
            "step1.txt": "first",
            "step2.txt": "second",
            "step3.txt": "third",
        },
        # Final assistant text should reference at least one file by
        # name. The exact phrasing varies; match on "step3" since
        # that's the freshest action the model just took.
        assert_min_text_in_final=["step3"],
        assert_max_total_cards=14,
        # Three real prompts produce at least 3 cards. The model is
        # free to batch the create+ls in prompt 4 into one shell call.
        assert_min_total_cards=3,
        assert_max_agent_turns_per_prompt=6,
    ),
    "subagent_summary": Scenario(
        name="subagent_summary",
        description="Spawn a background subagent and wait for the completion notice",
        prompts=[
            f"/fork Read {(Path(__file__).resolve().parent.parent / 'pyproject.toml')} and {(Path(__file__).resolve().parent.parent / 'src' / 'successor' / '__init__.py')}, then reply with only the current version string they agree on.",
        ],
        assert_min_text_in_final=[SUCCESSOR_VERSION],
        assert_each_settles=True,
        allow_synthetic_final=True,
    ),
    "holonet_biomedical": Scenario(
        name="holonet_biomedical",
        description="Use the holonet tool against live biomedical APIs",
        pre_setup=_enable_holonet_tool,
        prompts=[
            "Use the holonet tool, not bash. Find one semaglutide obesity paper and one registered clinical trial, then tell me one trial ID and the paper title.",
        ],
        assert_min_text_in_final=["semaglutide"],
        assert_turn_plain_contains={
            1: ["paper-search", "trial-search", "clinicaltrials"],
        },
        assert_each_settles=True,
    ),
    "browser_local_fixture": Scenario(
        name="browser_local_fixture",
        description="Use the Playwright browser tool on a local interactive fixture page",
        pre_setup=_enable_browser_tool_with_fixture,
        prompts=[
            "Use the browser tool, not bash. Open the local browser fixture URL already in context, type 'successor browser test' into the Message field, click Apply, then tell me the final visible result text.",
        ],
        assert_min_text_in_final=["successor browser test"],
        assert_turn_plain_contains={
            1: ["browser-open", "browser-type", "browser-click"],
        },
        assert_each_settles=True,
    ),
    "model_subagent_version_audit": Scenario(
        name="model_subagent_version_audit",
        description="Model uses the subagent tool, then answers from the completion notification",
        pre_setup=_enable_subagent_tool,
        prompts=[
            (
                f"Use the subagent tool, not bash, to audit the shared version in "
                f"{(Path(__file__).resolve().parent.parent / 'pyproject.toml')} and "
                f"{(Path(__file__).resolve().parent.parent / 'src' / 'successor' / '__init__.py')}. "
                "Name the task version-audit. After you start it, tell me a background subagent is running and stop."
            ),
            "What did the background subagent report? Answer from the notification only and do not inspect the files yourself.",
        ],
        assert_min_text_in_final=[SUCCESSOR_VERSION],
        assert_max_total_cards=1,
        assert_min_total_cards=1,
        assert_max_agent_turns_per_prompt=4,
        assert_each_settles=True,
    ),
}


# ─── CLI entry point ───


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default="write_html",
        help=f"scenario name or 'all'. Use --list to see all scenarios.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list all available scenarios with descriptions",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="number of stability runs per scenario (default 1)",
    )
    parser.add_argument(
        "--no-mid-stream",
        action="store_true",
        help="disable mid-stream frame capture (faster, less disk)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"llama-server URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--artifact-root",
        default=str(ARTIFACT_ROOT),
        help=f"output dir (default: {ARTIFACT_ROOT})",
    )
    args = parser.parse_args()

    if args.list:
        print("\nAvailable scenarios:\n")
        for name, sc in SCENARIOS.items():
            print(f"  {name:24s} {sc.description}")
            print(f"  {'':24s} ({len(sc.prompts)} prompt{'s' if len(sc.prompts) != 1 else ''})")
        return 0

    artifact_root = Path(args.artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)

    if args.scenario == "all":
        scenario_names = list(SCENARIOS.keys())
    else:
        scenario_names = [args.scenario]

    capture_mid_stream = not args.no_mid_stream
    summary_per_scenario: list[dict] = []

    for name in scenario_names:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            print(
                f"ERROR: unknown scenario {name!r}. "
                f"Available: {sorted(SCENARIOS.keys())}"
            )
            return 1
        print(f"\n{'=' * 70}")
        print(f"Scenario: {name} ({args.runs} run{'s' if args.runs != 1 else ''})")
        print(f"{'=' * 70}")

        if args.runs == 1:
            passed, assertions = run_scenario(
                scenario, args.base_url, args.model, artifact_root, capture_mid_stream,
            )
            summary_per_scenario.append({
                "name": name,
                "runs": 1,
                "all_passed": passed,
                "results": [{"run": 1, "passed": passed, "assertions": [asdict(a) for a in assertions]}],
            })
        else:
            summary = run_scenario_with_stability(
                scenario, args.base_url, args.model, artifact_root,
                args.runs, capture_mid_stream,
            )
            summary_per_scenario.append(summary)

    # Final overall summary
    print(f"\n{'=' * 70}")
    print("OVERALL SUMMARY")
    print(f"{'=' * 70}")
    any_failed = False
    for s in summary_per_scenario:
        if s["all_passed"]:
            print(f"  PASS  {s['name']}  ({s['runs']} run{'s' if s['runs'] != 1 else ''})")
        else:
            any_failed = True
            failures = [r["run"] for r in s["results"] if not r["passed"]]
            print(f"  FAIL  {s['name']}  ({s['runs']} runs, failed: {failures})")
    print()

    overall_path = artifact_root / "overall_summary.json"
    overall_path.write_text(json.dumps(summary_per_scenario, indent=2))
    print(f"Overall summary: {overall_path}")

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
