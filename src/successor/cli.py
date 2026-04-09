"""Successor command-line interface.

Subcommands:

    successor                — show help
    successor chat           — chat interface (real llama.cpp streaming)
    successor setup          — profile creation wizard with live preview
    successor config         — three-pane profile config menu
    successor doctor         — terminal capability check
    successor skills         — list loaded skills
    successor tools          — list registered tools
    successor record         — record an input session
    successor replay         — replay a recorded session
    successor snapshot       — headless render of a chat scenario
    successor bench          — renderer benchmark
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__


# ─── subcommands ───


def cmd_chat(args: argparse.Namespace) -> int:
    """Open the chat with re-entry support for the config menu.

    Plays the active profile's intro animation on first launch.
    Subsequent re-entries (after exiting the config menu) skip the
    intro to avoid being annoying. The chat's `_pending_action` flag
    is checked after each chat.run() to decide whether to open the
    config menu and then resume the chat.
    """
    from .chat import SuccessorChat
    from .profiles import get_active_profile
    from .wizard import run_config_menu

    profile = get_active_profile()
    first_launch = True

    while True:
        # Intro only on the very first launch in this session — re-entry
        # after the config menu skips it.
        if first_launch and profile.intro_animation:
            _play_intro_animation(profile.intro_animation)
        first_launch = False

        chat = SuccessorChat(profile=profile)
        chat.run()

        if getattr(chat, "_pending_action", None) == "config":
            # User opened the config menu from inside the chat. Run it,
            # then resume the chat with whichever profile they want
            # active when they exit.
            requested_name = run_config_menu()
            if requested_name:
                from .profiles import get_profile
                next_profile = get_profile(requested_name)
                if next_profile is not None:
                    profile = next_profile
                else:
                    profile = get_active_profile()
            else:
                # Cancelled — resume with whatever active_profile says
                profile = get_active_profile()
            continue

        # Normal exit — leave the loop
        break

    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Run the config menu standalone (not from inside the chat).

    On exit, drops into the chat with the user's selected active
    profile. If they cancel, drops into the chat with the previously-
    active profile.
    """
    from .chat import SuccessorChat
    from .profiles import get_active_profile, get_profile
    from .wizard import run_config_menu

    requested_name = run_config_menu()
    if requested_name:
        profile = get_profile(requested_name) or get_active_profile()
    else:
        profile = get_active_profile()

    SuccessorChat(profile=profile).run()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the profile creation wizard.

    Multi-step interactive wizard with a live preview pane that uses
    the actual chat renderer to show the user's choices in real time.
    On save, transitions directly into the chat with the new profile
    active.

    First-launch flourish: the SUCCESSOR emergence animation plays
    before the wizard opens, so a user typing `successor setup` for
    the first time sees the brand portrait morph in. Skippable with
    any keypress (the intro App handles that).
    """
    from .chat import SuccessorChat
    from .wizard import run_setup_wizard

    # Play the bundled SUCCESSOR intro animation BEFORE the wizard
    # opens. Always plays — first-time users will see this; repeat
    # users can skip with any key. If the bundled frames are missing
    # (broken install) the helper exits cleanly.
    _play_intro_animation("successor")

    saved_profile = run_setup_wizard()
    if saved_profile is None:
        # User cancelled — exit cleanly without launching the chat
        return 0

    # Drop straight into the chat with the new profile active. We
    # do NOT replay the intro here — the user already saw it before
    # the wizard, and watching it twice is annoying.
    SuccessorChat(profile=saved_profile).run()
    return 0


def _play_intro_animation(name: str) -> None:
    """Play a registered intro animation, blocking until it finishes.

    For v0, only "successor" is supported — it plays the bundled
    11-frame numbered emergence sequence ending on the title frame,
    held for a couple of seconds. `hero.txt` is separate empty-state
    art for the chat and is not part of this animation. Any keypress
    skips ahead. Unknown intro names are silently ignored so a profile
    that references a future intro doesn't break the chat.
    """
    if name != "successor":
        # Future: walk ~/.config/successor/intros/<name>/ for user intros.
        return
    from .intros import run_successor_intro

    try:
        run_successor_intro()
    except RuntimeError:
        # Frames dir missing on this install — skip silently.
        return


def cmd_skills(args: argparse.Namespace) -> int:
    """List every loaded skill (built-in + user) with size + source.

    Skills are selected per profile and loaded on demand by the chat's
    internal `skill` tool. This command is the inventory view: it shows
    what is installed, where it came from, and the approximate prompt
    budget once a skill is actually loaded.
    """
    from .skills import SKILL_REGISTRY

    SKILL_REGISTRY.reload()
    skills = SKILL_REGISTRY.all()
    if not skills:
        print("successor: no skills loaded")
        print(f"  drop *.md files into {SKILL_REGISTRY.kind}/ to add them:")
        from .loader import builtin_root, config_dir

        print(f"    builtin: {builtin_root() / 'skills'}")
        print(f"    user:    {config_dir() / 'skills'}")
        return 0

    print(f"successor · skills ({len(skills)} loaded)")
    print()
    name_w = max(len(s.name) for s in skills)
    total_tokens = sum(s.estimated_tokens for s in skills)
    for skill in skills:
        source = SKILL_REGISTRY.source_of(skill.name) or "?"
        tokens = skill.estimated_tokens
        print(f"  {skill.name:<{name_w}}  {source:>7}  ~{tokens:>5} tokens")
        if skill.description:
            # Soft-wrap the description to a comfortable column width
            desc = skill.description
            indent = " " * (4 + name_w)
            max_w = 80 - len(indent)
            while desc:
                if len(desc) <= max_w:
                    print(f"{indent}{desc}")
                    break
                # Break at the last space before max_w
                cut = desc.rfind(" ", 0, max_w)
                if cut < 0:
                    cut = max_w
                print(f"{indent}{desc[:cut]}")
                desc = desc[cut:].lstrip()
        indent = " " * (4 + name_w)
        if skill.when_to_use:
            print(f"{indent}when:  {skill.when_to_use}")
        if skill.allowed_tools:
            print(f"{indent}tools: {', '.join(skill.allowed_tools)}")
        print()
    print(f"  {len(skills)} skills · ~{total_tokens:,} tokens total")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """List Python-import tools from the ToolRegistry with source.

    This registry is distinct from the built-in `bash` capability,
    which is already wired into the chat via `tools_registry.py`.
    `successor tools` inventories the dynamic Python-import tool
    loader (`read_file`, future user tools, etc.), which is not yet
    dispatched by the chat loop.
    """
    from .tools import TOOL_REGISTRY

    TOOL_REGISTRY.reload()
    tools = TOOL_REGISTRY.all()
    if not tools:
        print("successor: no tools registered")
        print("  bash is still available via profiles + the chat tool path.")
        from .loader import builtin_root, config_dir

        print(f"  builtin: {builtin_root() / 'tools'}")
        print(f"  user:    {config_dir() / 'tools'}")
        return 0

    print(f"successor · tools ({len(tools)} registered)")
    print()
    name_w = max(len(t.name) for t in tools)
    for tool in tools:
        source = TOOL_REGISTRY.source_of(tool.name) or "?"
        print(f"  {tool.name:<{name_w}}  {source:>7}")
        if tool.description:
            indent = " " * (4 + name_w)
            print(f"{indent}{tool.description}")
        print()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import fcntl
    import struct
    import termios

    print(f"successor {__version__}")
    print(f"  python      {sys.version.split()[0]}")
    print(f"  platform    {sys.platform}")
    print()

    is_tty = sys.stdout.isatty()
    print(f"  stdout      {'tty' if is_tty else 'not a tty'}")
    if is_tty:
        try:
            data = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("hhhh", data)
            print(f"  size        {cols} cols × {rows} rows")
        except OSError as e:
            print(f"  size        unknown ({e})")

    print(f"  TERM        {os.environ.get('TERM', '<unset>')}")
    print(f"  COLORTERM   {os.environ.get('COLORTERM', '<unset>')}")
    truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    print(f"  truecolor   {'yes' if truecolor else 'no (renderer still emits 24-bit SGR)'}")
    print()

    from .render.measure import char_width, text_width

    samples = [
        ("ascii a",   "a"),
        ("CJK 中",    "中"),
        ("braille ⠿", "⠿"),
        ("emoji 🦊",  "🦊"),
        ("combine ñ", "n\u0303"),
    ]
    print("  measure cell widths:")
    for label, ch in samples:
        print(f"    {label:12s}  char_width={char_width(ch[0])}  text_width={text_width(ch)}")
    print()

    from .intros import successor_intro_frame_paths
    from .loader import builtin_root
    intro_dir = builtin_root() / "intros" / "successor"
    if intro_dir.exists():
        intro_frames = successor_intro_frame_paths(intro_dir)
        print(f"  intro       {len(intro_frames)} successor emergence frames")
        if (intro_dir / "hero.txt").exists():
            print(f"  hero        chat empty-state hero art present")
        print(f"  intro_dir   {intro_dir}")

    # ─── Active profile + provider connectivity check ───
    #
    # Useful when the user runs `successor doctor` to debug "why
    # isn't my chat working?" — the terminal capability dump alone
    # doesn't help. This section probes the active profile's
    # provider, reports whether the endpoint is reachable, and
    # surfaces the resolved context window so the user can verify
    # everything's wired before launching the chat.
    print()
    print("  active profile:")
    try:
        from .profiles import get_active_profile
        from .providers import make_provider
        from .web import (
            available_provider_status,
            browser_runtime_status,
            resolve_browser_config,
            resolve_holonet_config,
            resolve_vision_config,
            vision_runtime_status,
        )
        profile = get_active_profile()
        print(f"    name        {profile.name}")
        provider_cfg = profile.provider or {}
        provider_type = provider_cfg.get("type", "<unset>")
        base_url = provider_cfg.get("base_url", "<unset>")
        model = provider_cfg.get("model", "<unset>")
        api_key = provider_cfg.get("api_key", "")
        api_key_status = (
            "set"
            if isinstance(api_key, str) and api_key.strip()
            else "(none — local server)"
            if provider_type == "llamacpp"
            else "MISSING"
        )
        print(f"    provider    {provider_type}")
        print(f"    base_url    {base_url}")
        print(f"    model       {model}")
        print(f"    api_key     {api_key_status}")

        # Construct the client and probe it. Both probes are short
        # and tolerant of failure — if the server is down or the
        # auth is wrong we want a friendly status line, not a stack
        # trace.
        try:
            client = make_provider(provider_cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"    status      provider construction failed: {exc}")
            return 0

        # Health check (HTTP reachability)
        health_ok: bool | None = None
        try:
            health_ok = bool(getattr(client, "health", lambda: False)())
        except Exception:  # noqa: BLE001
            health_ok = False
        if health_ok:
            print(f"    status      reachable")
        elif health_ok is False:
            print(f"    status      UNREACHABLE — is the server running?")
        else:
            print(f"    status      unknown (no health check on this provider)")

        # Context window detection
        detect = getattr(client, "detect_context_window", None)
        if callable(detect):
            try:
                ctx = detect()
            except Exception:  # noqa: BLE001
                ctx = None
            override = provider_cfg.get("context_window")
            if isinstance(override, int) and override > 0:
                print(f"    ctx window  {override} tokens (profile override)")
            elif isinstance(ctx, int) and ctx > 0:
                print(f"    ctx window  {ctx} tokens (auto-detected)")
            else:
                print(f"    ctx window  unknown — falls back to 262144 default")

        detect_caps = getattr(client, "detect_runtime_capabilities", None)
        if callable(detect_caps):
            try:
                caps = detect_caps()
            except Exception:  # noqa: BLE001
                caps = None
            total_slots = getattr(caps, "total_slots", None)
            endpoint_slots = bool(getattr(caps, "endpoint_slots", False))
            parallel_tools = bool(
                getattr(caps, "supports_parallel_tool_calls", False)
            )
            if isinstance(total_slots, int) and total_slots > 0:
                slot_note = "/slots on" if endpoint_slots else "/slots off"
                print(f"    slots       {total_slots} total ({slot_note})")
            if caps is not None:
                print(
                    "    tool calls  "
                    + ("parallel supported" if parallel_tools else "serial only")
                )

        tools = tuple(profile.tools or ())
        if "holonet" in tools:
            holo_cfg = resolve_holonet_config(profile)
            holo_status = available_provider_status(holo_cfg)
            enabled = [name for name, ok in holo_status.items() if ok]
            disabled = [name for name, ok in holo_status.items() if not ok]
            print(f"    holonet     default={holo_cfg.default_provider}")
            print(
                "    holonet ok  "
                + (", ".join(enabled) if enabled else "none")
            )
            if disabled:
                print(
                    "    holonet off "
                    + ", ".join(disabled)
                )
        if "browser" in tools:
            browser_cfg = resolve_browser_config(profile)
            browser_status = browser_runtime_status(profile.name, browser_cfg)
            print(
                "    browser     "
                + ("playwright ready" if browser_status.package_available else "playwright missing")
            )
            runtime_note = " (external runtime)" if browser_status.using_external_runtime else ""
            print(f"    browser py  {browser_status.python_executable}{runtime_note}")
            print(f"    browser ch  {browser_status.channel or '(default chromium)'}")
            if browser_status.executable_path:
                print(f"    browser exe {browser_status.executable_path}")
            print(f"    browser dir {browser_status.user_data_dir}")
        if "vision" in tools:
            vision_cfg = resolve_vision_config(profile)
            vision_client = make_provider(profile.provider or {"type": "llamacpp"})
            vision_status = vision_runtime_status(vision_cfg, client=vision_client)
            print(
                "    vision      "
                + ("ready" if vision_status.tool_available else "unavailable")
                + f" ({vision_status.mode})"
            )
            if vision_status.provider_type:
                print(f"    vision type {vision_status.provider_type}")
            if vision_status.base_url:
                print(f"    vision url  {vision_status.base_url}")
            if vision_status.model:
                print(f"    vision mod  {vision_status.model}")
            print(f"    vision note {vision_status.reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"    error: could not load active profile ({exc})")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Run the chat with a recorder attached, saving every input byte.

    Use `successor replay <file>` afterward to play it back. The recording
    file is JSONL — one event per line — and can be inspected or
    hand-edited.
    """
    from .chat import SuccessorChat
    from .recorder import Recorder

    path = Path(args.output)
    print(f"recording to {path} (Ctrl+C to stop)")
    with Recorder(path) as rec:
        chat = SuccessorChat(recorder=rec)
        chat.run()
    print(f"\\nrecording saved: {path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay a recording into a fresh chat instance.

    Feeds the recorded bytes into the chat at original timing (or
    faster with --speed). Useful for bug repro and demos.

    NOTE: replay does NOT talk to the model — the recorded bytes are
    INPUT only (keystrokes), not the model's responses. The model
    will be re-queried during replay if the user submits messages,
    so the responses may differ. For deterministic playback, the
    consumer would need to also record the model output, which is
    deferred to a future commit.
    """
    from .chat import SuccessorChat
    from .recorder import Player

    path = Path(args.input)
    if not path.exists():
        print(f"successor: no such file: {path}", file=sys.stderr)
        return 1

    player = Player(path, speed=args.speed)
    chat = SuccessorChat()

    # Drive the chat by feeding bytes through on_key. We have to run
    # the chat's main loop ourselves so the renderer ticks during
    # playback. We use a simple thread to feed bytes while the chat's
    # main loop runs.
    import threading
    import time as time_mod

    def _feeder():
        # Wait for the chat to enter its terminal context
        time_mod.sleep(0.5)
        player.play_into(chat.on_key)
        # When playback is done, leave a short tail so the user can
        # see the final state, then call stop() to break the run loop.
        # stop() sets _running = False which the App.run() loop checks
        # at the next select wakeup.
        time_mod.sleep(1.0)
        chat.stop()

    feeder_thread = threading.Thread(target=_feeder, daemon=True)
    feeder_thread.start()
    chat.run()
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Render a chat scenario to ANSI / plain text without a TTY.

    Useful for marketing material, documentation, bug-repro screenshots,
    and verifying the renderer in CI without needing a real terminal.
    """
    from .snapshot import (
        chat_demo_snapshot,
        render_grid_to_ansi,
        render_grid_to_plain,
    )

    grid = chat_demo_snapshot(
        rows=args.rows,
        cols=args.cols,
        theme_name=args.theme,
        display_mode=args.display_mode,
        density_name=args.density,
        scenario=args.scenario,
    )

    if args.format == "ansi":
        output = render_grid_to_ansi(grid)
    else:
        output = render_grid_to_plain(grid) + "\n"

    if args.output == "-":
        sys.stdout.write(output)
    else:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(f"wrote {len(output):,} bytes to {path}")

    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .intros import successor_intro_frame_paths
    from .loader import builtin_root
    from .render.braille import interpolate_frame, load_frame
    from .render.cells import Grid, Style
    from .render.diff import diff_frames
    from .render.paint import fill_region, paint_lines

    rows = args.rows
    cols = args.cols
    n = args.frames

    print(f"successor bench: {n} frames at {cols}×{rows}")

    intro_dir = builtin_root() / "intros" / "successor"
    fr = successor_intro_frame_paths(intro_dir) if intro_dir.exists() else []
    if len(fr) < 2:
        print("successor: need at least 2 intro frames to bench", file=sys.stderr)
        return 1
    fa = load_frame(fr[0])
    fb = load_frame(fr[-1])

    front = Grid(rows, cols)
    back = Grid(rows, cols)
    bg = Style(bg=0x101010)
    fg = Style(fg=0xFF3030, bg=0x101010)

    total_bytes = 0
    t0 = time.perf_counter()
    for i in range(n):
        back.clear()
        fill_region(back, 0, 0, cols, rows, style=bg)
        t = (i % 60) / 60.0
        morphed = interpolate_frame(fa, fb, t)
        if morphed:
            x = max(0, (cols - len(morphed[0])) // 2)
            y = max(0, (rows - len(morphed)) // 2)
            paint_lines(back, morphed, x, y, style=fg)
        prev = None if i == 0 else front
        delta = diff_frames(prev, back)
        total_bytes += len(delta.encode("utf-8"))
        front, back = back, front
    elapsed = time.perf_counter() - t0

    fps = n / elapsed if elapsed > 0 else float("inf")
    print(f"  elapsed     {elapsed * 1000:.1f} ms")
    print(f"  fps         {fps:.0f}")
    print(f"  per-frame   {(elapsed / n) * 1000:.2f} ms")
    print(f"  bytes       {total_bytes:,} total ({total_bytes // n:,} avg)")
    print(f"  wire        {(total_bytes / elapsed) / 1024:.1f} KiB/s at full speed")
    return 0


# ─── argparse plumbing ───


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="successor",
        description=(
            "successor — terminal chat harness for local llama.cpp and "
            "OpenAI-compatible endpoints (OpenAI, OpenRouter, etc.)"
        ),
        epilog=(
            "First time? Run `successor setup` to create a profile with "
            "the wizard, or `successor chat` to chat against the default "
            "(local llama.cpp on http://localhost:8080)."
        ),
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"successor (successor) {__version__}",
    )

    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    p_chat = sub.add_parser(
        "chat",
        help="streaming chat with the active profile (default command for daily use)",
    )
    p_chat.set_defaults(func=cmd_chat)

    p_setup = sub.add_parser(
        "setup",
        help="10-step profile creation wizard with live preview pane",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_config = sub.add_parser(
        "config",
        help="three-pane profile config menu (browse + edit + live preview)",
    )
    p_config.set_defaults(func=cmd_config)

    p_doctor = sub.add_parser(
        "doctor",
        help="terminal + active profile health check",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_skills = sub.add_parser(
        "skills",
        help="list loaded skills (markdown frontmatter format)",
    )
    p_skills.set_defaults(func=cmd_skills)

    p_tools = sub.add_parser(
        "tools",
        help="list Python-import tools in the registry (separate from bash)",
    )
    p_tools.set_defaults(func=cmd_tools)

    p_bench = sub.add_parser("bench", help="renderer benchmark (no TTY required)")
    p_bench.add_argument("--frames", type=int, default=300, help="number of frames")
    p_bench.add_argument("--rows", type=int, default=40, help="grid rows")
    p_bench.add_argument("--cols", type=int, default=120, help="grid cols")
    p_bench.set_defaults(func=cmd_bench)

    p_snapshot = sub.add_parser(
        "snapshot",
        help="render a chat scenario to text/ANSI without a TTY",
    )
    p_snapshot.add_argument("--rows", type=int, default=30, help="grid rows")
    p_snapshot.add_argument("--cols", type=int, default=100, help="grid cols")
    # Theme is intentionally NOT a choices= list — it's resolved against
    # the live registry at run time so user-installed themes work without
    # editing argparse. Validation happens inside chat_demo_snapshot,
    # which falls back to the default theme if the name is unknown.
    p_snapshot.add_argument(
        "--theme", default="steel",
        help="color theme name (any registered theme — see `successor doctor`)",
    )
    p_snapshot.add_argument(
        "--display-mode", default="dark",
        choices=["dark", "light"],
        help="display mode within the chosen theme",
    )
    p_snapshot.add_argument(
        "--density", default="normal",
        choices=["compact", "normal", "spacious"],
        help="layout density",
    )
    p_snapshot.add_argument(
        "--scenario", default="showcase",
        choices=["blank", "showcase", "thinking", "search", "help", "autocomplete", "tool_card"],
        help="which chat state to render",
    )
    p_snapshot.add_argument(
        "--format", default="ansi",
        choices=["ansi", "text"],
        help="output format (ansi for `cat`, text for plain)",
    )
    p_snapshot.add_argument(
        "--output", "-o", default="-",
        help="output file or '-' for stdout",
    )
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_record = sub.add_parser(
        "record",
        help="run the chat with a recorder, saving every input byte",
    )
    p_record.add_argument("output", help="output recording file (JSONL)")
    p_record.set_defaults(func=cmd_record)

    p_replay = sub.add_parser(
        "replay",
        help="play back a recording into a fresh chat",
    )
    p_replay.add_argument("input", help="recording file to play back")
    p_replay.add_argument(
        "--speed", type=float, default=1.0,
        help="playback speed multiplier (1.0=real time, 2.0=2x, 0=instant)",
    )
    p_replay.set_defaults(func=cmd_replay)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
