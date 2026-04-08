"""Lightweight per-session runtime trace for normal chat sessions.

Writes JSONL events to `~/.config/successor/logs/` so postmortems can
inspect what the model did, which tool calls were spawned, and whether a
runner timed out, was cancelled, or never finished before exit.

The trace is local-only, append-only, and intentionally shallow:
small enough to leave on by default, rich enough to debug live hangs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .loader import config_dir


TRACE_DIR_NAME = "logs"
TRACE_KEEP_RECENT = 20


def trace_dir() -> Path:
    """Directory holding recent session trace JSONL files."""
    return config_dir() / TRACE_DIR_NAME


def _trace_path() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return trace_dir() / f"{stamp}-p{os.getpid()}.jsonl"


def clip_text(value: str, *, limit: int = 240) -> str:
    """One-line excerpt for trace payloads."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _prune_old_logs(root: Path) -> None:
    """Keep the trace directory bounded."""
    try:
        files = sorted(
            (p for p in root.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in files[TRACE_KEEP_RECENT:]:
        try:
            path.unlink()
        except OSError:
            pass


class SessionTrace:
    """Tiny JSONL event sink used by normal `successor chat` sessions."""

    def __init__(self) -> None:
        self.path = _trace_path()
        self._lock = threading.Lock()
        self._fp = None
        self._t0 = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _prune_old_logs(self.path.parent)
            self._fp = self.path.open("w", encoding="utf-8")
        except OSError:
            self._fp = None

    @property
    def enabled(self) -> bool:
        return self._fp is not None

    def emit(self, event_type: str, **payload: Any) -> None:
        if self._fp is None:
            return
        record = {
            "t": round(time.monotonic() - self._t0, 4),
            "type": event_type,
            **payload,
        }
        try:
            line = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
        except TypeError:
            return
        with self._lock:
            if self._fp is None:
                return
            try:
                self._fp.write(line)
                self._fp.flush()
            except OSError:
                self._fp = None

    def close(self, **payload: Any) -> None:
        if self._fp is None:
            return
        self.emit("session_end", **payload)
        with self._lock:
            if self._fp is None:
                return
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
