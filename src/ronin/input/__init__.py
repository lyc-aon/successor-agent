"""Ronin input parsing — bytes from stdin → typed input events.

The KeyDecoder feeds bytes one at a time and emits KeyEvents (for
keyboard input) or MouseEvents (for mouse input, when the terminal
has SGR mouse reporting enabled).
"""
from .keys import (
    InputEvent,
    Key,
    KeyDecoder,
    KeyEvent,
    MouseButton,
    MouseEvent,
    key_name,
)

__all__ = [
    "InputEvent",
    "Key",
    "KeyDecoder",
    "KeyEvent",
    "MouseButton",
    "MouseEvent",
    "key_name",
]
