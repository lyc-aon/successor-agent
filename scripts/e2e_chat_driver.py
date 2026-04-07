"""End-to-end chat driver — real multi-turn sessions against a live
llama-server, with per-turn plaintext / ANSI / message / workspace
snapshots for human review.

Not a pytest. Not a replacement for unit tests. A scripted smoke run
that reproduces real user interactions against real model output and
captures everything needed to audit the result.

Run:
    .venv/bin/python scripts/e2e_chat_driver.py --scenario write_html
    .venv/bin/python scripts/e2e_chat_driver.py --scenario all

Artifacts land at:
    /tmp/successor-e2e/<scenario>/
        workspace/           — the bash working_directory (real files)
        turn_01_plain.txt    — chat painted to a grid, ANSI stripped
        turn_01_ansi.txt     — full ANSI dump (cat to see colors)
        turn_01_messages.json  — every _Message at settle time
        turn_01_workspace.txt  — recursive listing of workspace/
        turn_01_loop.json    — agent_turn count, tool cards, timings
        session.log          — full driver log
        index.md             — turn-by-turn summary

Scenarios are defined in the SCENARIOS dict at the bottom of this
module. Each scenario is a list of user prompts plus a short name.
The driver runs each prompt end-to-end (including any agent-loop
continuation turns) and snapshots once the chat has fully settled.

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
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Make sure we import from the repo's src, not any installed copy
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from successor.chat import SuccessorChat  # noqa: E402
from successor.profiles import Profile  # noqa: E402
from successor.providers import make_provider  # noqa: E402
from successor.render.cells import Grid  # noqa: E402
from successor.snapshot import render_grid_to_ansi, render_grid_to_plain  # noqa: E402


# ─── Driver configuration ───

GRID_ROWS = 60
GRID_COLS = 140
TURN_TIMEOUT_S = 180.0  # per user prompt, including all continuation turns
TICK_SLEEP_S = 0.05  # how long to sleep between _pump_stream calls
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_MODEL = "qwopus"
ARTIFACT_ROOT = Path("/tmp/successor-e2e")


# ─── Per-scenario state ───


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


# ─── Core driver ───


def build_profile(workspace: Path, base_url: str, model: str) -> Profile:
    """Construct the yolo test profile the driver uses for every
    scenario. Bash enabled, mutating + dangerous both allowed,
    working_directory pinned to the scenario workspace.
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
        tool_config={
            "bash": {
                "allow_dangerous": True,
                "allow_mutating": True,
                "timeout_s": 30.0,
                "max_output_bytes": 8192,
                "working_directory": str(workspace),
            },
        },
        intro_animation=None,
    )


def settle_chat(chat: SuccessorChat, timeout_s: float) -> tuple[bool, float]:
    """Drive `_pump_stream` until the chat is fully settled — no
    stream in flight AND the agent-loop turn counter has dropped
    back to 0 (meaning continuation has finished or halted).

    Returns (settled_cleanly, elapsed_seconds). If the timeout
    trips, settled_cleanly is False and the caller decides what
    to do with the partial state.
    """
    start = time.monotonic()
    while True:
        if chat._stream is None and chat._agent_turn == 0:
            return True, time.monotonic() - start
        chat._pump_stream()
        if time.monotonic() - start > timeout_s:
            # Timeout — try to clean up cleanly so the next turn can
            # still proceed. Close any in-flight stream.
            if chat._stream is not None:
                try:
                    chat._stream.close()
                except Exception:
                    pass
                chat._stream = None
            chat._agent_turn = 0
            return False, time.monotonic() - start
        time.sleep(TICK_SLEEP_S)


def run_user_prompt(
    chat: SuccessorChat,
    prompt: str,
    turn_index: int,
) -> TurnStats:
    """Submit one user prompt, drain all agent-loop turns, return stats."""
    # Snapshot the message list BEFORE so we can diff after settling.
    # Tool cards added during this prompt can sit at any index — the
    # continue-loop appends text messages AFTER the cards — so slicing
    # from len(chat.messages) before to after captures everything.
    messages_before = len(chat.messages)

    # Wrap the client's stream_chat so we can count how many model
    # calls happened inside this prompt (turn 1 + any continuations).
    _install_call_counter(chat)
    pre_count = chat.client._driver_call_count

    chat.input_buffer = prompt
    chat._submit()
    settled, wall_s = settle_chat(chat, TURN_TIMEOUT_S)

    # Diff: every message added during this prompt
    new_msgs = chat.messages[messages_before:]
    new_cards = [m.tool_card for m in new_msgs if m.tool_card is not None]
    cards_new = len(new_cards)
    executed = sum(1 for c in new_cards if c.executed)
    refused = sum(1 for c in new_cards if not c.executed)

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
    )
    if not settled:
        stats.notes.append(f"timeout after {wall_s:.1f}s")
    if cards_new and agent_turns < 2:
        stats.notes.append(
            "suspicious: bash executed but no continuation turn fired"
        )
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

    # Plain + ANSI visual snapshot
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

    # Serialized message list
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
                "raw_command_preview": (card.raw_command or "")[:200],
                "params": list(card.params),
            }
        messages_payload.append(entry)
    (out_dir / f"{prefix}_messages.json").write_text(
        json.dumps(messages_payload, indent=2)
    )

    # Workspace tree
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

    # Loop / turn stats
    (out_dir / f"{prefix}_loop.json").write_text(json.dumps(asdict(stats), indent=2))


def write_index(out_dir: Path, scenario: str, all_stats: list[TurnStats]) -> None:
    """Turn-by-turn summary table for humans to skim."""
    lines = [
        f"# E2E scenario: {scenario}",
        "",
        f"Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| Turn | Prompt (trimmed) | Agent turns | Cards | Exec | Refused | Wall (s) | Notes |",
        "|------|------------------|-------------|-------|------|---------|----------|-------|",
    ]
    for s in all_stats:
        prompt_short = s.user_prompt.replace("\n", " ")
        if len(prompt_short) > 50:
            prompt_short = prompt_short[:47] + "…"
        note_str = "; ".join(s.notes) if s.notes else "-"
        lines.append(
            f"| {s.turn_index} | {prompt_short} | {s.agent_turns_consumed} | "
            f"{s.tool_cards_appended} | {s.tool_cards_executed} | "
            f"{s.tool_cards_refused} | {s.wall_clock_s:.1f} | {note_str} |"
        )
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- `turn_NN_plain.txt` — ANSI-stripped chat paint")
    lines.append("- `turn_NN_ansi.txt` — full ANSI dump (`cat` to see colors)")
    lines.append("- `turn_NN_messages.json` — every _Message at settle time")
    lines.append("- `turn_NN_workspace.txt` — recursive workspace listing")
    lines.append("- `turn_NN_loop.json` — stats for this user prompt")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")


# ─── Scenarios ───


SCENARIOS: dict[str, list[str]] = {
    "write_html": [
        "Create a file called about.html with a heading that says 'Successor' and a short paragraph about a Python TUI agent harness.",
        "Show me the contents of about.html to confirm it wrote correctly.",
    ],
    "read_verify": [
        "Write the string 'hello from successor' into a file called note.txt using printf.",
        "Read note.txt back and tell me what it says.",
    ],
    "grep_report": [
        "Create a file called colors.txt with three lines: red, green, blue.",
        "Use grep to find all lines in colors.txt that contain the letter 'e', then tell me how many matches there were.",
    ],
    "multi_step_build": [
        "Scaffold a minimal Python package in a subdirectory called tiny/: make the directory, then create an __init__.py and a main.py where main.py just prints 'ok'. Finally run python tiny/main.py to prove it works.",
    ],
    "error_recovery": [
        "Try to cat a file called does_not_exist.txt (it doesn't exist — I want to see how you handle the error).",
        "Now create does_not_exist.txt with the single word 'created' and cat it again.",
    ],
    "long_output": [
        "Use seq to print the numbers 1 through 25, then tell me if the last number you saw was 25.",
    ],
}


def run_scenario(
    scenario: str,
    base_url: str,
    model: str,
    artifact_root: Path,
) -> bool:
    """Run one named scenario end-to-end. Returns True on clean run."""
    prompts = SCENARIOS.get(scenario)
    if prompts is None:
        print(
            f"ERROR: unknown scenario {scenario!r}. "
            f"Available: {sorted(SCENARIOS.keys())}"
        )
        return False

    out_dir = artifact_root / scenario
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

    log(f"=== E2E scenario: {scenario} ===")
    log(f"workspace : {workspace}")
    log(f"artifacts : {out_dir}")
    log(f"base_url  : {base_url}")
    log(f"model     : {model}")
    log("")

    # Hermetic config dir
    config_dir = Path(tempfile.mkdtemp(prefix=f"successor-e2e-{scenario}-"))
    os.environ["SUCCESSOR_CONFIG_DIR"] = str(config_dir)

    # Build profile + chat. We construct the chat with no greeting so
    # snapshots show a clean history starting from turn 1.
    profile = build_profile(workspace, base_url, model)
    chat = SuccessorChat()
    chat.profile = profile
    chat.system_prompt = profile.system_prompt
    chat.client = make_provider(profile.provider)
    chat.messages = []

    all_stats: list[TurnStats] = []
    all_settled = True

    for turn_idx, prompt in enumerate(prompts, start=1):
        log(f"--- turn {turn_idx}: {prompt}")
        try:
            stats = run_user_prompt(chat, prompt, turn_idx)
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
            all_settled = False
        else:
            log(
                f"  agent_turns={stats.agent_turns_consumed} "
                f"cards={stats.tool_cards_appended} "
                f"exec={stats.tool_cards_executed} "
                f"refused={stats.tool_cards_refused} "
                f"wall={stats.wall_clock_s:.1f}s "
                f"settled={stats.settled_cleanly}"
            )
            if stats.notes:
                for n in stats.notes:
                    log(f"  NOTE: {n}")
            if not stats.settled_cleanly:
                all_settled = False

        dump_snapshots(chat, workspace, out_dir, stats)
        all_stats.append(stats)

    write_index(out_dir, scenario, all_stats)
    log("")
    log(f"=== scenario complete: settled={all_settled} ===")
    log_file.close()
    return all_settled


# ─── CLI entry point ───


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default="write_html",
        help=f"scenario name or 'all'. available: {sorted(SCENARIOS.keys())}",
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

    artifact_root = Path(args.artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)

    if args.scenario == "all":
        scenarios = list(SCENARIOS.keys())
    else:
        scenarios = [args.scenario]

    any_failed = False
    for name in scenarios:
        print(f"\n{'=' * 70}")
        print(f"Running scenario: {name}")
        print(f"{'=' * 70}")
        ok = run_scenario(name, args.base_url, args.model, artifact_root)
        if not ok:
            any_failed = True
            print(f"!! scenario {name} had timeouts / crashes — review artifacts")

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
