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
    from .demos.chat import RoninChat

    RoninChat().run()
    return 0


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

    p_bench = sub.add_parser("bench", help="renderer benchmark (no TTY required)")
    p_bench.add_argument("--frames", type=int, default=300, help="number of frames")
    p_bench.add_argument("--rows", type=int, default=40, help="grid rows")
    p_bench.add_argument("--cols", type=int, default=120, help="grid cols")
    p_bench.set_defaults(func=cmd_bench)

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
