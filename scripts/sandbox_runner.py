"""Sandbox runner — full-tool E2E testing against a live llama-server.

Extends the existing e2e_chat_driver with a richer profile (all native
tools enabled, yolo bash, real system prompt) and longer timeouts for
multi-step autonomous work.

Run:
    .venv/bin/python scripts/sandbox_runner.py --tier 1
    .venv/bin/python scripts/sandbox_runner.py --tier 1 --scenario hello-file
    .venv/bin/python scripts/sandbox_runner.py --list

Artifacts land at ~/.local/share/successor/sandbox/tier-{N}/{timestamp}_{scenario}/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Repo-local imports
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from successor.profiles import Profile  # noqa: E402
from successor.profiles.profile import CompactionConfig, SubagentConfig  # noqa: E402

# Import the E2E driver helpers we're reusing
from e2e_chat_driver import (  # noqa: E402
    DEFAULT_FRAME_INTERVAL_S,
    Scenario,
    AssertionResult,
    run_scenario,
)
from sandbox_scenarios import SANDBOX_SCENARIOS  # noqa: E402


# ─── Configuration ───

SANDBOX_ROOT = Path.home() / ".local" / "share" / "successor" / "sandbox"
DEFAULT_BASE_URL = os.environ.get("SANDBOX_BASE_URL", "http://localhost:8080")
DEFAULT_MODEL = os.environ.get("SANDBOX_MODEL", "Qwen3.6-35B-A3B")

# Longer timeouts — these are multi-step autonomous tasks
SANDBOX_TURN_TIMEOUT_S = float(
    os.environ.get("SANDBOX_TURN_TIMEOUT_S", "600.0")
)
SANDBOX_MAX_TOKENS = int(
    os.environ.get("SANDBOX_MAX_TOKENS", "61440")
)

SANDBOX_SYSTEM_PROMPT = """\
You are running inside successor, a terminal chat harness. The interface \
renders your replies live with full markdown: headers, lists, code fences, \
blockquotes, inline code, and links all paint correctly in the chat surface. \
Use them when they help clarity.

Be direct and brief. Lead with the answer, not the throat-clearing. Skip \
filler labels like "Sure!", "Of course", "Great question", "Solution:", \
"Verification:", "Note:", or trailing checkmark summaries. If a topic \
genuinely needs multiple distinct points, use a list; if it doesn't, write \
a sentence.

Use native file tools for normal authoring work: `read_file` to inspect \
files, `edit_file` for targeted changes, and `write_file` for new files or \
full rewrites. Use `bash` for shell and system work like tests, git, builds, \
or serving apps. Cite file paths as `file.py:123` when discussing code so \
the user can navigate. Show your reasoning when it helps the user follow \
along, hide it when it doesn't.\
"""


# ─── Profile builder ───


def build_sandbox_profile(
    workspace: Path,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
) -> Profile:
    """Full-tool yolo profile for sandbox testing.

    Unlike the E2E driver's bash-only profile, this enables the complete
    tool surface (file tools, bash, browser, vision, subagent, holonet)
    with dangerous commands allowed and a realistic system prompt.
    """
    return Profile(
        name="sandbox-yolo",
        description="full-tool sandbox testing — all tools, yolo bash",
        theme="steel",
        display_mode="dark",
        density="normal",
        system_prompt=SANDBOX_SYSTEM_PROMPT,
        provider={
            "type": "llamacpp",
            "base_url": base_url,
            "model": model,
            "max_tokens": SANDBOX_MAX_TOKENS,
            "temperature": 0.7,
        },
        skills=(
            "holonet-research",
            "browser-operator",
            "browser-verifier",
            "vision-inspector",
        ),
        tools=(
            "read_file",
            "write_file",
            "edit_file",
            "bash",
            "subagent",
            "holonet",
            "browser",
            "vision",
        ),
        tool_config={
            "bash": {
                "allow_dangerous": True,
                "allow_mutating": True,
                "timeout_s": 60.0,
                "max_output_bytes": 16384,
                "working_directory": str(workspace),
            },
            "holonet": {
                "default_provider": "auto",
                "brave_enabled": True,
                "brave_api_key_file": "~/.config/successor/secrets/brave-api-key",
                "firecrawl_enabled": True,
                "firecrawl_api_key_file": "~/.config/successor/secrets/firecrawl-api-key",
            },
        },
        intro_animation=None,
        chat_intro_art=None,
        compaction=CompactionConfig(
            warning_pct=0.25,
            autocompact_pct=0.125,
            blocking_pct=0.03,
            warning_floor=8000,
            autocompact_floor=4000,
            blocking_floor=1000,
            enabled=True,
            keep_recent_rounds=6,
            summary_max_tokens=16000,
        ),
        subagents=SubagentConfig(
            enabled=True,
            strategy="serial",
            max_model_tasks=1,
            notify_on_finish=True,
            timeout_s=900.0,
        ),
        max_agent_turns=999,
    )


# ─── Sandbox run wrapper ───


def run_sandbox_scenario(
    scenario: Scenario,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    *,
    tier: int = 1,
    capture_mid_stream: bool = True,
    frame_interval_s: float = DEFAULT_FRAME_INTERVAL_S,
) -> tuple[bool, list[AssertionResult]]:
    """Run one sandbox scenario with the full-tool profile.

    This patches the E2E driver's environment to use our extended
    timeout and then delegates to the standard run_scenario flow,
    but with a pre_setup hook that swaps in our sandbox profile.
    """
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    artifact_root = SANDBOX_ROOT / f"tier-{tier}"
    subdir = f"{timestamp}_{scenario.name}"

    # Patch the turn timeout for this run
    import e2e_chat_driver
    original_timeout = e2e_chat_driver.TURN_TIMEOUT_S
    e2e_chat_driver.TURN_TIMEOUT_S = SANDBOX_TURN_TIMEOUT_S

    # The E2E driver calls build_profile internally. We override the
    # profile via the scenario's pre_setup callback. The driver creates
    # a bash-only profile first, then pre_setup replaces it with our
    # full-tool profile. This avoids forking run_scenario.
    original_pre_setup = scenario.pre_setup

    def _sandbox_pre_setup(chat):
        workspace = Path(chat.profile.tool_config.get("bash", {}).get(
            "working_directory", "."
        ))
        profile = build_sandbox_profile(workspace, base_url, model)
        chat.profile = profile
        chat.system_prompt = profile.system_prompt
        # Re-create client with our provider config
        from successor.providers import make_provider
        chat.client = make_provider(profile.provider)
        if original_pre_setup is not None:
            original_pre_setup(chat)

    # Patch the scenario with our pre_setup
    from dataclasses import replace
    patched = replace(scenario, pre_setup=_sandbox_pre_setup)

    try:
        passed, assertions = run_scenario(
            patched,
            base_url,
            model,
            artifact_root,
            capture_mid_stream,
            frame_interval_s=frame_interval_s,
            subdir=subdir,
        )
    finally:
        e2e_chat_driver.TURN_TIMEOUT_S = original_timeout

    # Write observer metadata
    out_dir = artifact_root / subdir
    observer = {
        "timestamp": timestamp,
        "tier": tier,
        "scenario": scenario.name,
        "model": model,
        "base_url": base_url,
        "profile": "sandbox-yolo",
        "turn_timeout_s": SANDBOX_TURN_TIMEOUT_S,
        "max_tokens": SANDBOX_MAX_TOKENS,
        "passed": passed,
        "assertions": [asdict(a) for a in assertions],
    }
    (out_dir / "observer_meta.json").write_text(
        json.dumps(observer, indent=2)
    )

    return passed, assertions


# ─── CLI ───


def main():
    parser = argparse.ArgumentParser(
        description="Sandbox runner — full-tool E2E testing",
    )
    parser.add_argument(
        "--tier", type=int, choices=[1, 2, 3, 4],
        help="run all scenarios in a tier",
    )
    parser.add_argument(
        "--scenario", type=str,
        help="run a single scenario by name",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="list all available scenarios",
    )
    parser.add_argument(
        "--base-url", type=str, default=DEFAULT_BASE_URL,
        help=f"llama-server URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--no-mid-stream", action="store_true",
        help="disable mid-stream frame capture",
    )
    args = parser.parse_args()

    if args.list:
        print("Available sandbox scenarios:\n")
        for tier_num in sorted(SANDBOX_SCENARIOS.keys()):
            print(f"  Tier {tier_num}:")
            for s in SANDBOX_SCENARIOS[tier_num]:
                print(f"    {s.name:30s} — {s.description}")
        print()
        return

    if args.scenario:
        # Find the scenario across all tiers
        found = None
        found_tier = None
        for tier_num, scenarios in SANDBOX_SCENARIOS.items():
            for s in scenarios:
                if s.name == args.scenario:
                    found = s
                    found_tier = tier_num
                    break
            if found:
                break
        if found is None:
            print(f"Unknown scenario: {args.scenario}")
            print("Use --list to see available scenarios.")
            sys.exit(1)
        passed, _ = run_sandbox_scenario(
            found,
            args.base_url,
            args.model,
            tier=found_tier,
            capture_mid_stream=not args.no_mid_stream,
        )
        sys.exit(0 if passed else 1)

    if args.tier:
        scenarios = SANDBOX_SCENARIOS.get(args.tier, [])
        if not scenarios:
            print(f"No scenarios defined for tier {args.tier}")
            sys.exit(1)
        results = []
        for s in scenarios:
            print(f"\n{'=' * 60}")
            print(f"  Scenario: {s.name}")
            print(f"  {s.description}")
            print(f"{'=' * 60}\n")
            passed, assertions = run_sandbox_scenario(
                s,
                args.base_url,
                args.model,
                tier=args.tier,
                capture_mid_stream=not args.no_mid_stream,
            )
            results.append((s.name, passed))

        print(f"\n{'=' * 60}")
        print(f"  Tier {args.tier} Summary")
        print(f"{'=' * 60}")
        for name, passed in results:
            mark = "PASS" if passed else "FAIL"
            print(f"  [{mark}] {name}")
        total_passed = sum(1 for _, p in results if p)
        print(f"\n  {total_passed}/{len(results)} scenarios passed")
        sys.exit(0 if total_passed == len(results) else 1)

    parser.print_help()


if __name__ == "__main__":
    main()
