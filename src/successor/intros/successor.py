"""SuccessorIntro — the chill emergence animation that opens the chat.

Plays the bundled successor braille animation frames sequentially with
smooth Bayer-dot interpolation between adjacent frames, then holds the
final oracle frame for a couple of seconds before auto-exiting. Any
keypress skips ahead.

The animation frames live in `src/successor/builtin/intros/successor/`
as 11 numbered text files (`00-emerge.txt` through `10-title.txt`).
`hero.txt` lives in the same directory for the chat's empty-state
panel, but it is NOT part of the intro animation sequence. `10-title`
is a legacy filename now; the bundled sequence ends on the settled
oracle hold frame rather than title text.

Renderer features used:
  - BrailleArt prepare/layout cache (Pretext-shaped) — re-runs of the
    same target size are free
  - interpolate_frame() Bayer-dot morphing for smooth transitions
  - viewport-aware sizing via fit_dimensions
  - Active profile theme variant for chrome colors
  - App.elapsed for frame-perfect timing
  - Auto-exit via stop() when total duration elapsed
"""

from __future__ import annotations

from pathlib import Path

from ..loader import builtin_root
from ..profiles import get_active_profile
from ..render.app import App
from ..render.braille import (
    BrailleArt,
    fit_dimensions,
    interpolate_frame,
    load_frame,
)
from ..render.cells import ATTR_BOLD, ATTR_DIM, Grid, Style
from ..render.paint import fill_region, paint_lines, paint_text
from ..render.terminal import Terminal
from ..render.theme import (
    THEME_REGISTRY,
    ThemeVariant,
    find_theme_or_fallback,
    normalize_display_mode,
)


# ─── Tunables ───

# How long each emerge transition takes. 11 frames means 10 transitions,
# so total emerge time = 10 * EMERGE_PER_FRAME_S.
EMERGE_PER_FRAME_S = 0.32

# How long to hold the final oracle frame before auto-exiting.
HOLD_FINAL_S = 0.9

# Optional fade-in over the very first transition so the first emerge
# frame doesn't pop in hard.
FADE_IN_S = 0.4

# Padding around the centered braille art (cells of margin on each side).
# Smaller value = bigger art.
PAD_CELLS = 2


def _intro_frames_dir() -> Path:
    """Path to the bundled successor intro frame text files."""
    return builtin_root() / "intros" / "successor"


def successor_intro_frame_paths(directory: Path | None = None) -> list[Path]:
    """Ordered numbered frame files for the bundled successor intro.

    Excludes `hero.txt`, which is dedicated empty-state art for the
    chat surface rather than part of the startup animation.
    """
    frames_dir = directory if directory is not None else _intro_frames_dir()
    return sorted(frames_dir.glob("[0-9][0-9]-*.txt"))


class SuccessorIntro(App):
    """The bundled successor emergence intro.

    Constructed with a Terminal (or default), runs to completion or
    early-exit on any keypress, then returns. The cli `cmd_chat`
    main loop calls this before constructing the chat.
    """

    def __init__(
        self,
        *,
        terminal: Terminal | None = None,
    ) -> None:
        super().__init__(
            target_fps=30.0,
            quit_keys=b"\x03",  # Ctrl+C
            terminal=terminal if terminal is not None else Terminal(bracketed_paste=True),
        )

        # Resolve the active profile's theme so the intro paints in
        # the user's color palette (not a hardcoded one). Falls back
        # to steel/dark on any failure.
        try:
            THEME_REGISTRY.reload()
            profile = get_active_profile()
            theme = find_theme_or_fallback(profile.theme)
            self._variant: ThemeVariant = theme.variant(
                normalize_display_mode(profile.display_mode)
            )
        except Exception:
            steel = find_theme_or_fallback("steel")
            self._variant = steel.variant("dark")

        # Load only the numbered animation frames in filename order so
        # 00→10 is preserved. `hero.txt` is separate chat art, not an
        # animation frame.
        frames_dir = _intro_frames_dir()
        if not frames_dir.exists() or not frames_dir.is_dir():
            raise RuntimeError(f"successor intro frames dir missing: {frames_dir}")
        frame_paths = successor_intro_frame_paths(frames_dir)
        if not frame_paths:
            raise RuntimeError(f"no intro frames in {frames_dir}")
        self._arts: list[BrailleArt] = [BrailleArt(load_frame(p)) for p in frame_paths]

        # Total animation budget
        n = len(self._arts)
        self._n_transitions = max(0, n - 1)
        self._emerge_total_s = self._n_transitions * EMERGE_PER_FRAME_S
        self._hold_final_s = HOLD_FINAL_S
        self._total_s = self._emerge_total_s + self._hold_final_s

    # ─── Input ───

    def on_key(self, byte: int) -> None:
        # Any key skips the intro. Ctrl+C is also caught by the App
        # base class's quit_keys for consistency with everything else.
        self.stop()

    # ─── Per-frame state resolution ───

    def _resolve_frame_lines(self, grid: Grid) -> tuple[list[str], int, int]:
        """Compute the braille lines to paint this frame.

        Returns (frame_lines, cells_w, cells_h).

        Time model:
          - elapsed in [0, emerge_total_s): morph continuously across
            the full numbered frame sequence with no per-frame easing
            reset, so the motion doesn't "park" on every keyframe
          - elapsed in [emerge_total_s, total_s): hold the final frame
          - elapsed >= total_s: stop the App
        """
        elapsed = self.elapsed
        if elapsed >= self._total_s:
            self.stop()

        # Pick the viewport target size from the first art (all frames
        # are the same source size, so any of them works for sizing).
        first = self._arts[0]
        cells_w, cells_h = fit_dimensions(
            first.dot_h,
            first.dot_w,
            avail_cells_h=grid.rows - 4,  # leave space for the bottom label
            avail_cells_w=grid.cols,
            pad_cells=PAD_CELLS,
        )
        if cells_w <= 0 or cells_h <= 0:
            return ([], 0, 0)

        # Are we in the emerge phase or the hold phase?
        if elapsed >= self._emerge_total_s:
            # Holding the final frame
            final_lines = self._arts[-1].layout(cells_w, cells_h)
            return (final_lines, cells_w, cells_h)

        # Emerge phase — move linearly across the entire sequence so
        # interpolation stays in motion all the way through the oracle
        # reveal instead of easing into every numbered keyframe.
        idx_f = elapsed / EMERGE_PER_FRAME_S
        idx_a = int(idx_f)
        idx_b = min(idx_a + 1, len(self._arts) - 1)
        t = idx_f - idx_a

        a_lines = self._arts[idx_a].layout(cells_w, cells_h)
        b_lines = self._arts[idx_b].layout(cells_w, cells_h)
        morphed = interpolate_frame(a_lines, b_lines, t)
        return (morphed, cells_w, cells_h)

    # ─── Render ───

    def on_tick(self, grid: Grid) -> None:
        rows, cols = grid.rows, grid.cols
        if rows < 4 or cols < 8:
            self.stop()
            return

        # Background fill in the active profile's bg color
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=self._variant.bg))

        # Resolve the current frame
        frame_lines, cells_w, cells_h = self._resolve_frame_lines(grid)
        if not frame_lines:
            return

        # Center the braille block on the available area (above the
        # bottom label). Cell coordinates derived from grid.cols/rows
        # so resize works for free.
        block_h = len(frame_lines)
        block_w = max((len(line) for line in frame_lines), default=0)
        avail_h = rows - 4  # save room for the bottom label
        x = max(0, (cols - block_w) // 2)
        y = max(0, (avail_h - block_h) // 2)

        # Optional fade-in for the first FADE_IN_S of the animation —
        # lerps from bg toward accent. Avoids a hard pop on first paint.
        if self.elapsed < FADE_IN_S:
            fade_t = self.elapsed / FADE_IN_S
            from ..render.text import lerp_rgb
            ink = lerp_rgb(self._variant.bg, self._variant.accent, fade_t)
        else:
            ink = self._variant.accent

        paint_lines(
            grid,
            frame_lines,
            x, y,
            style=Style(fg=ink, bg=self._variant.bg, attrs=ATTR_BOLD),
        )

        # Subtle bottom-of-screen hint (only during emerge — once we
        # hit the hold phase the user has seen enough, no hint needed).
        if self.elapsed < self._emerge_total_s:
            hint = "press any key to skip"
            hint_x = max(0, (cols - len(hint)) // 2)
            hint_y = rows - 2
            if 0 <= hint_y < rows:
                paint_text(
                    grid, hint, hint_x, hint_y,
                    style=Style(
                        fg=self._variant.fg_subtle,
                        bg=self._variant.bg,
                        attrs=ATTR_DIM,
                    ),
                )


# ─── Public entry ───


def run_successor_intro(*, terminal: Terminal | None = None) -> None:
    """Run the intro to completion. Blocking. Returns when done.

    Use the `terminal` argument to share a Terminal context with the
    chat that will run after — saves a brief alt-screen flicker.
    """
    intro = SuccessorIntro(terminal=terminal)
    intro.run()
