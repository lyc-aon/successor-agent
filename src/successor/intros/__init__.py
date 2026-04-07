"""Intro animations — short braille flourishes that play before the chat opens.

A profile's `intro_animation` field names an intro to play. Currently
just `successor` (the bundled emergence animation that ends on a title
frame with the SUCCESSOR braille text). Built-in intros live in
`src/successor/builtin/intros/<name>/` as numbered frame text files.

The intro App plays frames sequentially with smooth Bayer-dot
interpolation between adjacent frames, then holds the final frame for
a couple of seconds before auto-exiting. Any keypress skips ahead.
"""

from .successor import SuccessorIntro, run_successor_intro

__all__ = ["SuccessorIntro", "run_successor_intro"]
