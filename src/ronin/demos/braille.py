"""Braille animation demos.

RoninDemo  — full keyframe sequence with Bayer-dithered morphs
RoninShow  — render a single static braille frame in the same chrome

Both subclass `App` so they share the renderer's frame loop, double
buffering, resize handling, and signal-safe terminal restore.

Both also use `BrailleArt` from render.braille — the Pretext-shaped
prepare/layout primitive — so the source frames are parsed exactly
once at startup, then re-laid-out on every terminal resize without
re-parsing. Layout results are cached per target size; resize triggers
exactly one resample pass per visible frame.
"""

from __future__ import annotations

from pathlib import Path

from ..render.app import App
from ..render.braille import (
    BrailleArt,
    fit_dimensions,
    interpolate_frame,
    load_frame,
)
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

# Number of cells reserved for chrome:
#   - 2 rows above the art (title + subtitle)
#   - 1 row below the art (status bar)
CHROME_TOP_ROWS = 2
CHROME_BOTTOM_ROWS = 1


def _paint_chrome(grid: Grid, *, subtitle: str, status_left: str, status_right: str) -> tuple[int, int]:
    """Paint title row, subtitle row, and status bar.

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

    return (CHROME_TOP_ROWS + 1, max(CHROME_TOP_ROWS + 1, rows - CHROME_BOTTOM_ROWS))


def _paint_ronin_block(grid: Grid, lines: list[str], top: int, bottom: int) -> None:
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


def _viewport_target(art: BrailleArt, grid: Grid) -> tuple[int, int]:
    """Pick the target braille cell size for the current viewport.

    Subtracts chrome rows from the available height before fitting,
    and asks `fit_dimensions` for the largest aspect-preserving cell-
    aligned rectangle.
    """
    avail_h = grid.rows - CHROME_TOP_ROWS - CHROME_BOTTOM_ROWS
    avail_w = grid.cols
    return fit_dimensions(
        art.dot_h,
        art.dot_w,
        avail_cells_h=avail_h,
        avail_cells_w=avail_w,
        pad_cells=1,
    )


# ─── RoninDemo: animated keyframe sequence with viewport scaling ───


class RoninDemo(App):
    def __init__(
        self,
        *,
        target_fps: float = 30.0,
        assets_dir: Path,
        sequence: tuple[str, ...] = DEFAULT_SEQUENCE,
        max_duration_s: float | None = None,
        intro_mode: bool = False,
        terminal=None,
    ) -> None:
        """The full nusamurai keyframe animation, optionally one-shot.

        max_duration_s: if set, the demo auto-exits after that many
            seconds. Used by the profile intro animation feature where
            the demo plays for a brief opening flourish before the chat
            takes over.
        intro_mode: if True, ANY keypress (not just q/Ctrl+C) exits the
            demo. Used as the intro variant so the user can skip past
            the intro by tapping any key. Disables the space-to-pause
            behavior since pausing during a brief intro doesn't make
            sense.
        terminal: optional Terminal instance to share with another App
            (e.g. the chat App that runs after the intro). If None, a
            default Terminal is constructed by App.__init__.
        """
        super().__init__(target_fps=target_fps, terminal=terminal)
        self.assets_dir = assets_dir
        self.max_duration_s = max_duration_s
        self.intro_mode = intro_mode
        self.arts: list[BrailleArt] = []
        self.names: list[str] = []
        for name in sequence:
            self.arts.append(BrailleArt(load_frame(assets_dir / f"{name}-ascii-art.txt")))
            self.names.append(name)
        if not self.arts:
            raise RuntimeError(f"No frames loaded from {assets_dir}")
        self.cycle_s = len(self.arts) * (HOLD_S + TRANS_S)
        # Animation clock — separate from App.elapsed (which is wall time).
        # We advance _anim_t only when not paused, so pausing freezes the
        # animation but the renderer keeps ticking (so resize / fps still
        # update during a pause).
        self._paused = False
        self._anim_t = 0.0
        self._last_live: float | None = None

    def on_key(self, byte: int) -> None:
        # In intro mode, ANY keypress exits — the intro is meant to be
        # skippable. Outside intro mode, only the App base class's
        # quit_keys (q/Q/Ctrl+C) exit, and space/p toggles pause.
        if self.intro_mode:
            self.stop()
            return
        # Space, p, P → toggle pause. Lets the user select+copy braille
        # without the morph overwriting cells under the selection.
        if byte in (0x20, 0x70, 0x50):
            self._paused = not self._paused

    def _advance_anim(self) -> None:
        now = self.elapsed
        if self._last_live is None:
            self._last_live = now
            return
        if not self._paused:
            self._anim_t += now - self._last_live
        self._last_live = now

    def _current(self, grid: Grid) -> tuple[list[str], str, str, float, tuple[int, int]]:
        """Return (frame_lines, name_a, name_b, t, (cells_w, cells_h))."""
        cells_w, cells_h = _viewport_target(self.arts[0], grid)
        if cells_w <= 0 or cells_h <= 0:
            return ([], self.names[0], self.names[0], 0.0, (0, 0))

        n = len(self.arts)
        ct = self._anim_t % self.cycle_s
        seg = HOLD_S + TRANS_S
        idx = int(ct // seg)
        offset = ct - idx * seg

        a_lines = self.arts[idx].layout(cells_w, cells_h)
        name_a = self.names[idx]

        if offset < HOLD_S:
            return (a_lines, name_a, name_a, 0.0, (cells_w, cells_h))

        next_idx = (idx + 1) % n
        b_lines = self.arts[next_idx].layout(cells_w, cells_h)
        name_b = self.names[next_idx]
        t = (offset - HOLD_S) / TRANS_S
        return (interpolate_frame(a_lines, b_lines, t), name_a, name_b, t, (cells_w, cells_h))

    def on_tick(self, grid: Grid) -> None:
        # Auto-exit when the intro duration has elapsed. Checked at the
        # top of the tick so we exit cleanly before painting another
        # frame the user will never see.
        if self.max_duration_s is not None and self.elapsed >= self.max_duration_s:
            self.stop()
        self._advance_anim()
        rows, cols = grid.rows, grid.cols
        frame_lines, name_a, name_b, t, (cw, ch) = self._current(grid)
        if name_a == name_b:
            phase = f"hold  {name_a}"
        else:
            phase = f"morph {name_a} → {name_b}  {int(t * 100):3d}%"
        fps_actual = self.frame / max(1e-6, self.elapsed)
        prefix = "PAUSED · " if self._paused else ""
        left = f" ronin · {prefix}{phase} "
        right = f" {cw}×{ch} art · {cols}×{rows} term · {fps_actual:5.1f} fps "
        subtitle = "— pos-th30 nusamurai · bayer dot interp · viewport-scaled · space=pause q=quit —"
        top, bottom = _paint_chrome(
            grid,
            subtitle=subtitle,
            status_left=left,
            status_right=right,
        )
        _paint_ronin_block(grid, frame_lines, top, bottom)


# ─── RoninShow: single static frame, viewport-scaled ───


class RoninShow(App):
    def __init__(self, *, name: str, path: Path) -> None:
        # Static content — low frame rate is enough.
        super().__init__(target_fps=10.0)
        self.pose_name = name
        self.art = BrailleArt(load_frame(path))

    def on_tick(self, grid: Grid) -> None:
        rows, cols = grid.rows, grid.cols
        cells_w, cells_h = _viewport_target(self.art, grid)
        lines = self.art.layout(cells_w, cells_h) if (cells_w > 0 and cells_h > 0) else []
        subtitle = f"— static · {self.pose_name} · viewport-scaled · q to quit —"
        left = f" ronin · show {self.pose_name} "
        right = f" {cells_w}×{cells_h} art · {cols}×{rows} term "
        top, bottom = _paint_chrome(
            grid,
            subtitle=subtitle,
            status_left=left,
            status_right=right,
        )
        _paint_ronin_block(grid, lines, top, bottom)
