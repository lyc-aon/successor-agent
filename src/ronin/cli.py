"""Ronin command-line interface.

The project is "Ronin" but the installed binary is `rn` (the `ronin`
binary name is heavily contested in the open-source ecosystem).

    rn                — show help
    rn chat           — chat interface (v0, scripted ronin responses)
    rn demo           — braille animation
    rn show <name>    — render a single static braille frame
    rn frames         — list available braille frames
    rn doctor         — terminal capabilities and renderer info
    rn bench          — renderer benchmark
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__


def _assets_root() -> Path:
    """Locate assets/ relative to the source tree.

    Phase 0 expects a development install (pip install -e . or running
    directly from a checkout). The path walks from src/ronin/cli.py up to
    the repo root.
    """
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "assets"


def _nusamurai_dir() -> Path:
    return _assets_root() / "nusamurai" / "pos-th30"


def _list_frames() -> list[tuple[str, Path]]:
    """Return sorted [(name, path)] of available braille frames."""
    d = _nusamurai_dir()
    if not d.exists():
        return []
    out: list[tuple[str, Path]] = []
    for p in sorted(d.glob("*-ascii-art.txt")):
        out.append((p.name.replace("-ascii-art.txt", ""), p))
    return out


# ─── subcommands ───


def cmd_chat(args: argparse.Namespace) -> int:
    """Open the chat with re-entry support for the config menu.

    Plays the active profile's intro animation on first launch.
    Subsequent re-entries (after exiting the config menu) skip the
    intro to avoid being annoying. The chat's `_pending_action` flag
    is checked after each chat.run() to decide whether to open the
    config menu and then resume the chat.
    """
    from .demos.chat import RoninChat
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

        chat = RoninChat(profile=profile)
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
    from .demos.chat import RoninChat
    from .profiles import get_active_profile, get_profile
    from .wizard import run_config_menu

    requested_name = run_config_menu()
    if requested_name:
        profile = get_profile(requested_name) or get_active_profile()
    else:
        profile = get_active_profile()

    RoninChat(profile=profile).run()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the profile creation wizard.

    Multi-step interactive wizard with a live preview pane that uses
    the actual chat renderer to show the user's choices in real time.
    On save, transitions directly into the chat with the new profile
    active.
    """
    from .demos.chat import RoninChat
    from .wizard import run_setup_wizard

    saved_profile = run_setup_wizard()
    if saved_profile is None:
        # User cancelled — exit cleanly without launching the chat
        return 0

    # Optionally play the intro animation the user just configured,
    # then drop into the chat with the new profile active.
    if saved_profile.intro_animation:
        _play_intro_animation(saved_profile.intro_animation)
    RoninChat(profile=saved_profile).run()
    return 0


def _play_intro_animation(name: str) -> None:
    """Play a registered intro animation, blocking until it finishes.

    For v0, only "nusamurai" is supported — it plays the bundled
    9-frame braille demo for ~4 seconds in one-shot mode (any keypress
    skips ahead). Unknown intro names are silently ignored so a profile
    that references a future intro doesn't break the chat.
    """
    if name != "nusamurai":
        # Future: walk ~/.config/ronin/intros/<name>/ for user intros.
        return
    from .demos.braille import RoninDemo

    try:
        intro = RoninDemo(
            target_fps=30.0,
            assets_dir=_nusamurai_dir(),
            max_duration_s=4.0,
            intro_mode=True,
        )
    except RuntimeError:
        # Asset dir missing on this install — skip the intro silently.
        return
    intro.run()


def cmd_demo(args: argparse.Namespace) -> int:
    from .demos.braille import RoninDemo

    demo = RoninDemo(target_fps=args.fps, assets_dir=_nusamurai_dir())
    demo.run()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .demos.braille import RoninShow

    frames = dict(_list_frames())
    if not frames:
        print(f"ronin: no frames in {_nusamurai_dir()}", file=sys.stderr)
        return 1
    name = args.name
    if name not in frames:
        print(f"ronin: no frame named '{name}'", file=sys.stderr)
        print(f"  available: {', '.join(sorted(frames))}", file=sys.stderr)
        return 1
    RoninShow(name=name, path=frames[name]).run()
    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    from .render.braille import load_frame

    frames = _list_frames()
    if not frames:
        print(f"ronin: no frames in {_nusamurai_dir()}", file=sys.stderr)
        return 1
    print(f"{len(frames)} braille frames in assets/nusamurai/pos-th30:")
    print()
    for name, path in frames:
        f = load_frame(path)
        h = len(f)
        w = max((len(line) for line in f), default=0)
        print(f"  {name:30s}  {w:>3} × {h:<3}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    """List every loaded skill (built-in + user) with size + source.

    Inventory only — phase 5 deliberately doesn't wire skills into the
    chat. The list shows what's available so the user can confirm their
    `~/.config/ronin/skills/*.md` files are being picked up.
    """
    from .skills import SKILL_REGISTRY

    SKILL_REGISTRY.reload()
    skills = SKILL_REGISTRY.all()
    if not skills:
        print("ronin: no skills loaded")
        print(f"  drop *.md files into {SKILL_REGISTRY.kind}/ to add them:")
        from .loader import builtin_root, config_dir

        print(f"    builtin: {builtin_root() / 'skills'}")
        print(f"    user:    {config_dir() / 'skills'}")
        return 0

    print(f"ronin · skills ({len(skills)} loaded)")
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
        print()
    print(f"  {len(skills)} skills · ~{total_tokens:,} tokens total")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """List every registered tool (built-in + user) with source.

    Phase 6 scaffold: the tool registry exists, the @tool decorator
    works, but no tools are actually wired into the chat (no agent
    loop yet). This command shows what would be available when the
    agent loop lands.
    """
    from .tools import TOOL_REGISTRY

    TOOL_REGISTRY.reload()
    tools = TOOL_REGISTRY.all()
    if not tools:
        print("ronin: no tools registered")
        print("  the agent loop is not yet wired (phase 6+).")
        from .loader import builtin_root, config_dir

        print(f"  builtin: {builtin_root() / 'tools'}")
        print(f"  user:    {config_dir() / 'tools'}")
        return 0

    print(f"ronin · tools ({len(tools)} registered)")
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

    print(f"ronin {__version__}")
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

    frames = _list_frames()
    print(f"  assets      {len(frames)} braille frames in nusamurai/pos-th30")
    print(f"  assets_dir  {_nusamurai_dir()}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Run the chat with a recorder attached, saving every input byte.

    Use `rn replay <file>` afterward to play it back. The recording
    file is JSONL — one event per line — and can be inspected or
    hand-edited.
    """
    from .demos.chat import RoninChat
    from .recorder import Recorder

    path = Path(args.output)
    print(f"recording to {path} (Ctrl+C to stop)")
    with Recorder(path) as rec:
        chat = RoninChat(recorder=rec)
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
    from .demos.chat import RoninChat
    from .recorder import Player

    path = Path(args.input)
    if not path.exists():
        print(f"ronin: no such file: {path}", file=sys.stderr)
        return 1

    player = Player(path, speed=args.speed)
    chat = RoninChat()

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
    from .render.braille import interpolate_frame, load_frame
    from .render.cells import Grid, Style
    from .render.diff import diff_frames
    from .render.paint import fill_region, paint_lines

    rows = args.rows
    cols = args.cols
    n = args.frames

    print(f"ronin bench: {n} frames at {cols}×{rows}")

    fr = _list_frames()
    if len(fr) < 2:
        print("ronin: need at least 2 frames to bench", file=sys.stderr)
        return 1
    fa = load_frame(fr[0][1])
    fb = load_frame(fr[1][1])

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
        prog="rn",
        description="rn — Ronin agent harness for locally-run mid-grade models",
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"rn (ronin) {__version__}",
    )

    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    p_chat = sub.add_parser("chat", help="chat interface (v0, scripted)")
    p_chat.set_defaults(func=cmd_chat)

    p_setup = sub.add_parser(
        "setup",
        help="profile creation wizard with live preview",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_config = sub.add_parser(
        "config",
        help="three-pane profile config menu (browse + edit + live preview)",
    )
    p_config.set_defaults(func=cmd_config)

    p_demo = sub.add_parser("demo", help="braille animation demo")
    p_demo.add_argument("--fps", type=float, default=30.0, help="target FPS (default 30)")
    p_demo.set_defaults(func=cmd_demo)

    p_show = sub.add_parser("show", help="render a single static braille frame")
    p_show.add_argument("name", help="frame name (see `ronin frames`)")
    p_show.set_defaults(func=cmd_show)

    p_frames = sub.add_parser("frames", help="list available braille frames")
    p_frames.set_defaults(func=cmd_frames)

    p_doctor = sub.add_parser("doctor", help="terminal capability check")
    p_doctor.set_defaults(func=cmd_doctor)

    p_skills = sub.add_parser(
        "skills",
        help="list loaded skills (markdown frontmatter format)",
    )
    p_skills.set_defaults(func=cmd_skills)

    p_tools = sub.add_parser(
        "tools",
        help="list registered tools (phase 6 scaffold — not yet wired)",
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
        help="color theme name (any registered theme — see `rn doctor`)",
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
        choices=["blank", "showcase", "thinking", "search", "help", "autocomplete"],
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
