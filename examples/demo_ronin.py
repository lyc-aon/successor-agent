"""Ronin braille animation demo.

Loads the nusamurai pos-th30 braille frames and cycles through them with
Bayer-dithered transitions, rendered through Ronin's renderer with no
calls to print(), Rich, prompt_toolkit, or any other rendering library.

Press q (or Ctrl+C) to exit. Drag the terminal corner to test resize.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ronin.render.app import App
from ronin.render.braille import interpolate_frame, load_frame
from ronin.render.cells import ATTR_BOLD, ATTR_DIM, Grid, Style
from ronin.render.paint import fill_region, paint_lines, paint_text


# ─── Palette (samurai red on near-black) ───
INK_DEEP   = 0x10070A   # background
INK_BLOOD  = 0xC1272D   # primary ronin red
INK_EMBER  = 0xFF6347   # warmer accent
INK_BONE   = 0xE6D9B8   # off-white text
INK_DUST   = 0x6B5A4A   # dim chrome
INK_SHADOW = 0x3A1418   # subtitle / border tint


ASSETS = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "nusamurai"
    / "pos-th30"
)


# Keyframe sequence — order chosen for narrative flow:
# meditate → unsheathe → form/strike → recover → display → torii (rest).
SEQUENCE: tuple[str, ...] = (
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

HOLD_S = 1.4    # how long each keyframe rests
TRANS_S = 0.55  # how long the dot-dither morph takes


class RoninDemo(App):
    def __init__(self) -> None:
        super().__init__(target_fps=30.0)
        self.frames: list[list[str]] = []
        self.names: list[str] = []
        for name in SEQUENCE:
            self.frames.append(load_frame(ASSETS / f"{name}-ascii-art.txt"))
            self.names.append(name)
        if not self.frames:
            raise RuntimeError(f"No frames loaded from {ASSETS}")
        self.cycle_s = len(self.frames) * (HOLD_S + TRANS_S)

    # ─── animation state ───

    def _current(self) -> tuple[list[str], str, str, float]:
        """Return (rendered_lines, name_a, name_b, t_in_morph)."""
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

    # ─── render ───

    def on_tick(self, grid: Grid) -> None:
        rows, cols = grid.rows, grid.cols
        bg = Style(bg=INK_DEEP)
        fill_region(grid, 0, 0, cols, rows, style=bg)

        # Title row
        title = "  ronin  ·  terminal renderer demo  "
        title_style = Style(fg=INK_BONE, bg=INK_DEEP, attrs=ATTR_BOLD)
        if rows >= 1:
            tx = max(0, (cols - len(title)) // 2)
            paint_text(grid, title, tx, 0, style=title_style)

        # Subtitle row
        hint = "— pos-th30 nusamurai keyframes · bayer dot interp · q to quit —"
        hint_style = Style(fg=INK_DUST, bg=INK_DEEP, attrs=ATTR_DIM)
        if rows >= 2:
            hx = max(0, (cols - len(hint)) // 2)
            paint_text(grid, hint, hx, 1, style=hint_style)

        # Braille frame, centered in the available middle region.
        frame_lines, name_a, name_b, t = self._current()
        avail_top = 3
        avail_bottom = rows - 1  # leave 1 line for status bar
        avail_h = avail_bottom - avail_top
        if avail_h > 0 and frame_lines:
            block_h = len(frame_lines)
            block_w = max((len(line) for line in frame_lines), default=0)
            x = max(0, (cols - block_w) // 2)
            y = avail_top + max(0, (avail_h - block_h) // 2)
            ronin_style = Style(fg=INK_BLOOD, bg=INK_DEEP)
            paint_lines(grid, frame_lines, x, y, style=ronin_style)

        # Status bar
        if rows >= 1:
            sb_y = rows - 1
            sb_style = Style(bg=INK_BLOOD, fg=INK_BONE, attrs=ATTR_BOLD)
            fill_region(grid, 0, sb_y, cols, 1, style=sb_style)
            fps_actual = self.frame / max(1e-6, self.elapsed)
            if name_a == name_b:
                phase = f"hold  {name_a}"
            else:
                phase = f"morph {name_a} → {name_b}  {int(t * 100):3d}%"
            left = f" ronin · {phase} "
            right = f" frame {self.frame}  {fps_actual:5.1f} fps  {cols}×{rows} "
            paint_text(grid, left, 0, sb_y, style=sb_style)
            rx = max(0, cols - len(right))
            paint_text(grid, right, rx, sb_y, style=sb_style)


if __name__ == "__main__":
    RoninDemo().run()
