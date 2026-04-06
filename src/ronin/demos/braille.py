"""Braille animation demos.

RoninDemo  — full keyframe sequence with Bayer-dithered morphs
RoninShow  — render a single static braille frame in the same chrome

Both subclass `App` so they share the renderer's frame loop, double
buffering, resize handling, and signal-safe terminal restore.
"""

from __future__ import annotations

from pathlib import Path

from ..render.app import App
from ..render.braille import interpolate_frame, load_frame
from ..render.cells import ATTR_BOLD, ATTR_DIM, Grid, Style
from ..render.paint import fill_region, paint_lines, paint_text


# ─── Palette (samurai red on near-black) ───
INK_DEEP = 0x10070A
INK_BLOOD = 0xC1272D
INK_EMBER = 0xFF6347
INK_BONE = 0xE6D9B8
INK_DUST = 0x6B5A4A
INK_SHADOW = 0x3A1418


# Default keyframe order — narrative flow:
# meditate → unsheathe → form/strike → recover → display → torii (rest).
DEFAULT_SEQUENCE: tuple[str, ...] = (
    "Meditating",
    "Unsheath",
    "StraightFrontHeadChest",
    "RightSlash",
    "RightSlashOverhead",
    "LeftPowerSlash",
    "RightProfileClose",
    "Weaponrack",
    "Torii",
)

HOLD_S = 1.4
TRANS_S = 0.55


def _paint_chrome(grid: Grid, *, subtitle: str, status_left: str, status_right: str) -> tuple[int, int]:
    """Paint the title row, subtitle row, and status bar.

    Returns (top_avail, bottom_avail) — the y range available for the
    main content (excluding chrome).
    """
    rows, cols = grid.rows, grid.cols
    fill_region(grid, 0, 0, cols, rows, style=Style(bg=INK_DEEP))

    title = "  ronin  ·  terminal renderer  "
    title_style = Style(fg=INK_BONE, bg=INK_DEEP, attrs=ATTR_BOLD)
    if rows >= 1:
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

    hint_style = Style(fg=INK_DUST, bg=INK_DEEP, attrs=ATTR_DIM)
    if rows >= 2:
        hx = max(0, (cols - len(subtitle)) // 2)
        paint_text(grid, subtitle, hx, 1, style=hint_style)

    if rows >= 1:
        sb_y = rows - 1
        sb_style = Style(bg=INK_BLOOD, fg=INK_BONE, attrs=ATTR_BOLD)
        fill_region(grid, 0, sb_y, cols, 1, style=sb_style)
        paint_text(grid, status_left, 0, sb_y, style=sb_style)
        rx = max(0, cols - len(status_right))
        paint_text(grid, status_right, rx, sb_y, style=sb_style)

    return (3, max(3, rows - 1))


def _paint_ronin_block(
    grid: Grid,
    lines: list[str],
    top: int,
    bottom: int,
) -> None:
    """Center a braille block in the [top, bottom) row range."""
    if not lines or bottom <= top:
        return
    block_h = len(lines)
    block_w = max((len(line) for line in lines), default=0)
    cols = grid.cols
    avail_h = bottom - top
    x = max(0, (cols - block_w) // 2)
    y = top + max(0, (avail_h - block_h) // 2)
    paint_lines(grid, lines, x, y, style=Style(fg=INK_BLOOD, bg=INK_DEEP))


# ─── RoninDemo: animated keyframe sequence ───


class RoninDemo(App):
    def __init__(
        self,
        *,
        target_fps: float = 30.0,
        assets_dir: Path,
        sequence: tuple[str, ...] = DEFAULT_SEQUENCE,
    ) -> None:
        super().__init__(target_fps=target_fps)
        self.assets_dir = assets_dir
        self.frames: list[list[str]] = []
        self.names: list[str] = []
        for name in sequence:
            self.frames.append(load_frame(assets_dir / f"{name}-ascii-art.txt"))
            self.names.append(name)
        if not self.frames:
            raise RuntimeError(f"No frames loaded from {assets_dir}")
        self.cycle_s = len(self.frames) * (HOLD_S + TRANS_S)

    def _current(self) -> tuple[list[str], str, str, float]:
        n = len(self.frames)
        ct = self.elapsed % self.cycle_s
        seg = HOLD_S + TRANS_S
        idx = int(ct // seg)
        offset = ct - idx * seg
        a = self.frames[idx]
        name_a = self.names[idx]
        if offset < HOLD_S:
            return (a, name_a, name_a, 0.0)
        next_idx = (idx + 1) % n
        b = self.frames[next_idx]
        name_b = self.names[next_idx]
        t = (offset - HOLD_S) / TRANS_S
        return (interpolate_frame(a, b, t), name_a, name_b, t)

    def on_tick(self, grid: Grid) -> None:
        rows, cols = grid.rows, grid.cols
        frame_lines, name_a, name_b, t = self._current()
        if name_a == name_b:
            phase = f"hold  {name_a}"
        else:
            phase = f"morph {name_a} → {name_b}  {int(t * 100):3d}%"
        fps_actual = self.frame / max(1e-6, self.elapsed)
        left = f" ronin · {phase} "
        right = f" frame {self.frame}  {fps_actual:5.1f} fps  {cols}×{rows} "
        subtitle = "— pos-th30 nusamurai · bayer dot interp · q to quit —"
        top, bottom = _paint_chrome(
            grid,
            subtitle=subtitle,
            status_left=left,
            status_right=right,
        )
        _paint_ronin_block(grid, frame_lines, top, bottom)


# ─── RoninShow: single static frame ───


class RoninShow(App):
    def __init__(self, *, name: str, path: Path) -> None:
        # Static content — low frame rate is enough.
        super().__init__(target_fps=10.0)
        self.pose_name = name
        self.art = load_frame(path)

    def on_tick(self, grid: Grid) -> None:
        rows, cols = grid.rows, grid.cols
        h = len(self.art)
        w = max((len(line) for line in self.art), default=0)
        subtitle = f"— static · {self.pose_name} · q to quit —"
        left = f" ronin · show {self.pose_name} "
        right = f" {w}×{h} art  ·  {cols}×{rows} term "
        top, bottom = _paint_chrome(
            grid,
            subtitle=subtitle,
            status_left=left,
            status_right=right,
        )
        _paint_ronin_block(grid, self.art, top, bottom)
