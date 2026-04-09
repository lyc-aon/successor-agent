"""Tests for the startup intro frame selection and skip handoff."""

from __future__ import annotations

from pathlib import Path

from successor.intros import SuccessorIntro, successor_intro_frame_paths


def test_successor_intro_frame_paths_excludes_hero(tmp_path: Path) -> None:
    intro_dir = tmp_path / "successor"
    intro_dir.mkdir()
    for name in ("00-emerge.txt", "01-emerge.txt", "10-title.txt", "hero.txt"):
        (intro_dir / name).write_text("⠿\n", encoding="utf-8")

    paths = successor_intro_frame_paths(intro_dir)
    assert [p.name for p in paths] == [
        "00-emerge.txt",
        "01-emerge.txt",
        "10-title.txt",
    ]


def test_successor_intro_frame_paths_returns_numbered_frames_in_order(
    tmp_path: Path,
) -> None:
    intro_dir = tmp_path / "successor"
    intro_dir.mkdir()
    for name in ("10-title.txt", "02-emerge.txt", "00-emerge.txt", "hero.txt"):
        (intro_dir / name).write_text("⠿\n", encoding="utf-8")

    paths = successor_intro_frame_paths(intro_dir)
    assert [p.name for p in paths] == [
        "00-emerge.txt",
        "02-emerge.txt",
        "10-title.txt",
    ]


def test_intro_preserves_leading_slash_on_skip() -> None:
    intro = SuccessorIntro.__new__(SuccessorIntro)
    intro._forward_input = ""
    stopped = {"value": False}

    def _stop() -> None:
        stopped["value"] = True

    intro.stop = _stop  # type: ignore[method-assign]

    SuccessorIntro.on_key(intro, ord("/"))

    assert intro._forward_input == "/"
    assert stopped["value"] is True


def test_intro_ignores_other_skip_keys() -> None:
    intro = SuccessorIntro.__new__(SuccessorIntro)
    intro._forward_input = ""
    stopped = {"value": False}

    def _stop() -> None:
        stopped["value"] = True

    intro.stop = _stop  # type: ignore[method-assign]

    SuccessorIntro.on_key(intro, ord(" "))

    assert intro._forward_input == ""
    assert stopped["value"] is True
