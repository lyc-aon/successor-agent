"""Session recording and replay.

The renderer is fully deterministic — `on_tick(grid)` is a pure
function of `(state, time, grid_size)`. That means we can record a
session as a sequence of (timestamp, key event) pairs and replay it
later by feeding the same events to a fresh App.

Two pieces:

  Recorder — wraps an existing chat App and writes every input byte
             to a JSONL file with monotonic timestamps.

  Player   — reads a recording file and feeds bytes back to a fresh
             chat App, sleeping between events to recreate the
             original timing (or as fast as possible if `--fast` is
             passed).

The recording format is intentionally simple JSON-per-line so files
can be inspected and edited by hand. Each line is one event:

    {"t": 1.234, "k": "a"}        printable ASCII
    {"t": 1.345, "k": "\\u0003"}  Ctrl+C
    {"t": 1.456, "k": "\\u001b[A"}  Up arrow

Special metadata events appear with no `k` field:

    {"t": 0, "type": "header", "version": 1, "started_at": "2026-04-..."}
    {"t": 1.999, "type": "resize", "rows": 30, "cols": 100}

For v0 we only record key bytes (the dominant input). Mouse and
resize events are deferred — they could be added by the same pattern
when needed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class _Event:
    t: float       # seconds since recording start
    keys: bytes    # the bytes received from stdin


class Recorder:
    """Append-only recorder. One file per session.

    Use as a context manager:

        with Recorder("session.jsonl") as rec:
            chat = SuccessorChat()
            chat.term._on_key_extra = rec.record_byte  # see chat hook
            chat.run()

    Or call .record_byte(b) directly from a wrapper.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._t0: float = 0.0
        self._fp = None

    def __enter__(self) -> "Recorder":
        self._t0 = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w", encoding="utf-8")
        header = {
            "t": 0.0,
            "type": "header",
            "version": 1,
        }
        self._fp.write(json.dumps(header) + "\n")
        self._fp.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fp is not None:
            try:
                footer = {"t": time.monotonic() - self._t0, "type": "end"}
                self._fp.write(json.dumps(footer) + "\n")
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def record_byte(self, b: int) -> None:
        """Append a single byte to the recording."""
        if self._fp is None:
            return
        try:
            event = {
                "t": round(time.monotonic() - self._t0, 4),
                "k": chr(b) if 0x20 <= b < 0x7F else f"\\x{b:02x}",
                "b": b,
            }
            self._fp.write(json.dumps(event) + "\n")
            # Don't flush every byte — it's wasteful. Flush on exit.
        except Exception:
            pass


def load_recording(path: str | Path) -> list[_Event]:
    """Read a recording file into a list of _Event objects.

    Skips header/footer/metadata lines.
    """
    p = Path(path)
    events: list[_Event] = []
    with p.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "type" in obj:
                continue  # header / footer / metadata
            t = float(obj.get("t", 0.0))
            b = obj.get("b")
            if b is None:
                continue
            events.append(_Event(t=t, keys=bytes([int(b)])))
    return events


class Player:
    """Replay a recording into a fresh chat App.

    Driver is "feed bytes at the original timestamps." Optionally
    speed up via the speed multiplier (1.0 = real time, 0.0 = as fast
    as possible).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        speed: float = 1.0,
    ) -> None:
        self.events = load_recording(path)
        self.speed = max(0.0, speed)

    def play_into(self, on_byte) -> None:
        """Feed each recorded byte to `on_byte(b)` at the original timing.

        on_byte is called once per byte. The caller is responsible for
        feeding it through their own input handler / decoder / etc.
        """
        if not self.events:
            return
        start = time.monotonic()
        for ev in self.events:
            if self.speed > 0:
                target_wall = start + (ev.t / self.speed)
                wait = target_wall - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            for b in ev.keys:
                on_byte(b)
