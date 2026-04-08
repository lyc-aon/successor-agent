"""Intro / hero art loader for the chat empty state.

The chat's empty-state painter shows a braille hero portrait on the
left of the chat area before the user has sent any messages. This
module is the resolver: given a name or path from the active profile's
`chat_intro_art` field, return a `BrailleArt` ready to lay out at any
target cell size.

Resolution order:

  1. Absolute path → load directly. Lets users reference any file on
     their system without registering it.
  2. Built-in name → look in `src/successor/builtin/intros/<name>/`
     for the canonical hero file. The lookup preference is:
       (a) `hero.txt` — the dedicated empty-state hero art (preferred)
       (b) `10-title.txt` — fallback for legacy intros that don't ship
           a hero.txt (the bundled successor intro had this convention
           before hero.txt landed)
  3. User dir → look in `~/.config/successor/art/<name>.txt` for a
     bare braille frame.
  4. Built-in single-file → look in
     `src/successor/builtin/intros/<name>.txt` (future-proof for art
     that doesn't have a full animation directory).

Returns None for any name that can't be resolved or any file that
fails to parse. The chat falls back to painting the info panel
without a hero, gracefully. The chat still works either way.

The loader is pure: no caching here. The chat instance caches the
loaded BrailleArt on `self._intro_art` so we don't hit disk every
frame; that's the chat's concern, not ours.
"""

from __future__ import annotations

import os
from pathlib import Path

from .braille import BrailleArt, load_frame


def load_intro_art(name_or_path: str | None) -> BrailleArt | None:
    """Resolve a profile's `chat_intro_art` field to a BrailleArt.

    Returns None if the field is None/empty, the path doesn't exist,
    or the file fails to parse. Callers should be ready to render
    without a hero.
    """
    if not name_or_path:
        return None
    if not isinstance(name_or_path, str):
        return None
    name = name_or_path.strip()
    if not name:
        return None

    # 1. Absolute path
    if name.startswith("/") or name.startswith("~"):
        path = Path(name).expanduser()
        return _try_load(path)

    # 2. Built-in name with full animation directory (e.g. "successor")
    # Prefer the dedicated hero.txt if it exists, fall back to the
    # legacy 10-title.txt convention for any older intro directory
    # that hasn't been updated yet.
    from ..loader import builtin_root, config_dir
    builtin_dir = builtin_root() / "intros" / name
    hero_path = builtin_dir / "hero.txt"
    if hero_path.exists():
        return _try_load(hero_path)
    title_path = builtin_dir / "10-title.txt"
    if title_path.exists():
        return _try_load(title_path)

    # 3. User dir
    user_path = config_dir() / "art" / f"{name}.txt"
    if user_path.exists():
        return _try_load(user_path)

    # 4. Built-in single-file (no directory)
    builtin_single = builtin_root() / "intros" / f"{name}.txt"
    if builtin_single.exists():
        return _try_load(builtin_single)

    return None


def _try_load(path: Path) -> BrailleArt | None:
    """Load a braille frame file and wrap it in a BrailleArt.

    Returns None on any failure (read error, parse error, empty frame).
    Callers should treat None as "no hero, paint the info panel only".
    """
    try:
        source = load_frame(path)
    except Exception:  # noqa: BLE001
        return None
    if not source:
        return None
    try:
        return BrailleArt(source)
    except Exception:  # noqa: BLE001
        return None
