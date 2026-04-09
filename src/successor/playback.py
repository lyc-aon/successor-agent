"""Self-contained recording bundles and playback viewer generation.

Normal `successor record` sessions and the E2E harness both want the
same thing: a durable artifact bundle with input bytes, rendered frame
timeline, trace events, and a browser-openable scrubber.

This module keeps that logic in one place so the product path and the
test harness do not drift.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from html import escape
from pathlib import Path
from typing import Any

from .recorder import Recorder
from .render.cells import Grid
from .snapshot import render_grid_to_plain


RECORDINGS_DIR_ENV = "SUCCESSOR_RECORDINGS_DIR"
DEFAULT_RECORDINGS_DIR = Path.home() / ".local" / "share" / "successor" / "recordings"


def recordings_dir() -> Path:
    """Default root for user-facing recording bundles."""
    env = os.environ.get(RECORDINGS_DIR_ENV)
    if env:
        return Path(env)
    return DEFAULT_RECORDINGS_DIR


def default_recording_bundle_dir() -> Path:
    """Timestamped default output directory for `successor record`."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return recordings_dir() / stamp


def latest_recording_bundle_dir() -> Path | None:
    """Most recent bundle dir under the default recordings root."""
    root = recordings_dir()
    if not root.exists():
        return None
    candidates = [path for path in root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_trace_events(source: str | Path) -> list[dict[str, object]]:
    """Load JSONL trace events from a file or directory."""
    path = Path(source)
    if path.is_dir():
        files = sorted(path.glob("*.jsonl"))
    elif path.exists():
        files = [path]
    else:
        return []

    events: list[dict[str, object]] = []
    for file_path in files:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            obj["_source"] = file_path.name
            obj["_line"] = line_no
            events.append(obj)
    return events


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _git_toplevel_for(path: Path) -> Path | None:
    """Return the enclosing git worktree root, if any."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    if not text:
        return None
    return Path(text)


def _git_dir_for_worktree(root: Path) -> Path | None:
    """Resolve the git dir for a worktree root, handling .git files too."""
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.lower().startswith(prefix):
        return None
    gitdir = text[len(prefix):].strip()
    resolved = Path(gitdir)
    if not resolved.is_absolute():
        resolved = (root / resolved).resolve()
    return resolved


def ensure_bundle_is_gitignored(bundle_root: str | Path) -> None:
    """Keep a local recording bundle out of git if it's inside a repo."""
    bundle_path = Path(bundle_root).resolve()
    probe = bundle_path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    worktree = _git_toplevel_for(probe)
    if worktree is None:
        return
    try:
        rel = bundle_path.relative_to(worktree)
    except ValueError:
        return
    git_dir = _git_dir_for_worktree(worktree)
    if git_dir is None:
        return
    exclude_path = git_dir / "info" / "exclude"
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8").splitlines() if exclude_path.exists() else []
    except OSError:
        existing = []
    entry = rel.as_posix().rstrip("/") + "/"
    if entry not in existing:
        try:
            with exclude_path.open("a", encoding="utf-8") as fp:
                if existing and not existing[-1].endswith("\n"):
                    fp.write("\n")
                fp.write(f"{entry}\n")
        except OSError:
            pass


def _closest_frame_index(frames: list[dict[str, object]], target_s: float) -> int:
    best_idx = 0
    best_delta = float("inf")
    for idx, frame in enumerate(frames):
        try:
            frame_t = float(frame.get("scenario_elapsed_s", 0.0))
        except (TypeError, ValueError):
            frame_t = 0.0
        delta = abs(frame_t - target_s)
        if delta < best_delta:
            best_idx = idx
            best_delta = delta
    return best_idx


def _as_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _artifact_entry(bundle_root: Path, path: Path, *, kind: str) -> dict[str, object]:
    rel = path.relative_to(bundle_root).as_posix()
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    return {
        "name": path.name,
        "href": rel,
        "kind": kind,
        "size_bytes": size_bytes,
    }


def _discover_bundle_artifacts(bundle_root: str | Path | None) -> dict[str, object]:
    if bundle_root is None:
        return {
            "primary": [],
            "turn_files": [],
            "images": [],
            "hidden_counts": {},
        }

    root = Path(bundle_root)
    if not root.exists() or not root.is_dir():
        return {
            "primary": [],
            "turn_files": [],
            "images": [],
            "hidden_counts": {},
        }

    primary_names = [
        "index.md",
        "summary.json",
        "session_trace.json",
        "session_trace.jsonl",
        "timeline.json",
        "input.jsonl",
        "session.log",
        "assertions.json",
    ]
    primary = [
        _artifact_entry(root, root / name, kind="primary")
        for name in primary_names
        if (root / name).exists()
    ]

    turn_files: list[dict[str, object]] = []
    for path in sorted(root.glob("turn_*")):
        if not path.is_file():
            continue
        turn_files.append(_artifact_entry(root, path, kind="turn"))
    turn_file_total = len(turn_files)
    turn_files = turn_files[:10]

    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
    images: list[dict[str, object]] = []
    hidden_stream_frames = 0
    hidden_stream_dirs = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "/turn_" in rel and "_stream/" in rel:
            hidden_stream_frames += 1
            continue
        if path.suffix.lower() not in image_exts:
            continue
        if path.name == "playback.html":
            continue
        images.append(_artifact_entry(root, path, kind="image"))
    for path in sorted(root.glob("turn_*_stream")):
        if path.is_dir():
            hidden_stream_dirs += 1

    return {
        "primary": primary,
        "turn_files": turn_files,
        "images": images[:18],
        "hidden_counts": {
            "hidden_stream_dirs": hidden_stream_dirs,
            "hidden_stream_frames": hidden_stream_frames,
            "omitted_turn_files": max(0, turn_file_total - 10),
            "omitted_images": max(0, len(images) - 18),
        },
    }


def _build_turn_summaries(frames: list[dict[str, object]]) -> list[dict[str, object]]:
    turns: dict[int, dict[str, object]] = {}
    for idx, frame in enumerate(frames):
        turn = _as_int(frame.get("turn_index", 0))
        if turn <= 0:
            continue
        elapsed_s = _as_float(frame.get("scenario_elapsed_s", 0.0))
        summary = turns.setdefault(
            turn,
            {
                "turn_index": turn,
                "first_frame_idx": idx,
                "last_frame_idx": idx,
                "start_s": elapsed_s,
                "end_s": elapsed_s,
                "frame_count": 0,
                "live_frames": 0,
                "idle_frames": 0,
                "stream_frames": 0,
                "tool_frames": 0,
                "max_agent_turn": 0,
                "max_running_tools": 0,
                "message_count_end": 0,
            },
        )
        summary["last_frame_idx"] = idx
        summary["start_s"] = min(_as_float(summary.get("start_s")), elapsed_s)
        summary["end_s"] = max(_as_float(summary.get("end_s")), elapsed_s)
        summary["frame_count"] = _as_int(summary.get("frame_count")) + 1
        if str(frame.get("kind", "")) == "live":
            summary["live_frames"] = _as_int(summary.get("live_frames")) + 1
        else:
            summary["idle_frames"] = _as_int(summary.get("idle_frames")) + 1
        if bool(frame.get("stream_open")):
            summary["stream_frames"] = _as_int(summary.get("stream_frames")) + 1
        running_tools = _as_int(frame.get("running_tools", 0))
        if running_tools > 0:
            summary["tool_frames"] = _as_int(summary.get("tool_frames")) + 1
        summary["max_running_tools"] = max(
            _as_int(summary.get("max_running_tools", 0)),
            running_tools,
        )
        summary["max_agent_turn"] = max(
            _as_int(summary.get("max_agent_turn", 0)),
            _as_int(frame.get("agent_turn", 0)),
        )
        summary["message_count_end"] = _as_int(frame.get("message_count", 0))

    ordered: list[dict[str, object]] = []
    for turn in sorted(turns):
        summary = turns[turn]
        summary["duration_s"] = round(
            max(0.0, _as_float(summary.get("end_s")) - _as_float(summary.get("start_s"))),
            4,
        )
        ordered.append(summary)
    return ordered


def _build_playback_payload(
    *,
    title: str,
    description: str,
    frames: list[dict[str, object]],
    trace_events: list[dict[str, object]],
    bundle_root: str | Path | None,
) -> dict[str, object]:
    turns = sorted(
        {
            _as_int(frame.get("turn_index", 0) or 0)
            for frame in frames
            if _as_int(frame.get("turn_index", 0) or 0) > 0
        }
    )
    turn_summaries = _build_turn_summaries(frames)
    event_type_counts: dict[str, int] = {}
    for event in trace_events:
        name = str(event.get("type") or "event")
        event_type_counts[name] = event_type_counts.get(name, 0) + 1

    duration_s = _as_float(frames[-1].get("scenario_elapsed_s", 0.0)) if frames else 0.0
    stats = {
        "frame_count": len(frames),
        "trace_event_count": len(trace_events),
        "turn_count": len(turns),
        "duration_s": round(duration_s, 4),
        "live_frame_count": sum(1 for frame in frames if str(frame.get("kind", "")) == "live"),
        "stream_frame_count": sum(1 for frame in frames if bool(frame.get("stream_open"))),
        "tool_frame_count": sum(1 for frame in frames if _as_int(frame.get("running_tools", 0)) > 0),
        "max_agent_turn": max((_as_int(frame.get("agent_turn", 0)) for frame in frames), default=0),
    }
    return {
        "title": title,
        "description": description,
        "frames": frames,
        "trace_events": trace_events,
        "turns": turns,
        "turn_summaries": turn_summaries,
        "event_type_counts": event_type_counts,
        "stats": stats,
        "artifacts": _discover_bundle_artifacts(bundle_root),
    }


def write_playback_html(
    output: str | Path,
    *,
    title: str,
    description: str,
    frames: list[dict[str, object]],
    trace_events: list[dict[str, object]],
    bundle_root: str | Path | None = None,
) -> Path:
    """Write a self-contained HTML session reviewer for recorded bundles."""
    output_path = Path(output)
    if output_path.suffix.lower() != ".html":
        output_path = output_path / "playback.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = _build_playback_payload(
        title=title,
        description=description,
        frames=frames,
        trace_events=trace_events,
        bundle_root=bundle_root,
    )
    payload_json = (
        json.dumps(payload)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    safe_title = escape(title)
    safe_description = escape(description)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #06080d;
      --surface: #0d121a;
      --surface-2: #121927;
      --surface-3: #182130;
      --border: #233046;
      --border-strong: #355074;
      --text: #ebf1fb;
      --dim: #a4b2c8;
      --muted: #73829a;
      --accent: #87c7ff;
      --accent-strong: #4db1ff;
      --accent-soft: rgba(135, 199, 255, 0.14);
      --success: #8be2b0;
      --warn: #f4bf6c;
      --danger: #ff8b99;
      --shadow: 0 18px 60px rgba(0, 0, 0, 0.45);
      --mono: "Iosevka Term", "IBM Plex Mono", "Geist Mono", ui-monospace, monospace;
      --sans: "Outfit", "Avenir Next", "Segoe UI", system-ui, sans-serif;
      --ease: cubic-bezier(0.22, 1, 0.36, 1);
      --dur: 0.2s;
    }}
    * {{ box-sizing: border-box; }}
    html {{ -webkit-font-smoothing: antialiased; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(92, 160, 255, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(228, 170, 74, 0.12), transparent 24%),
        linear-gradient(180deg, #0a0d14 0%, var(--bg) 46%, #04060a 100%);
      color: var(--text);
      font-family: var(--sans);
      overflow-x: hidden;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.22;
      mix-blend-mode: overlay;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.05'/%3E%3C/svg%3E");
    }}
    a {{
      color: inherit;
      text-decoration: none;
    }}
    button, select, input {{
      font: inherit;
    }}
    .shell {{
      position: relative;
      z-index: 1;
      max-width: 1680px;
      margin: 0 auto;
      padding: 28px;
    }}
    .hero {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 20px;
    }}
    .eyebrow {{
      color: var(--accent);
      font-size: 11px;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      margin-bottom: 12px;
      font-family: var(--mono);
    }}
    .hero h1 {{
      margin: 0 0 10px;
      font-size: clamp(2rem, 4vw, 3.6rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }}
    .hero p {{
      margin: 0;
      max-width: 70ch;
      color: var(--dim);
      line-height: 1.6;
      font-size: 15px;
    }}
    .hero-meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .pill {{
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.03);
      color: var(--dim);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 12px;
      font-family: var(--mono);
    }}
    .grid {{
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr) 360px;
      gap: 18px;
      align-items: start;
    }}
    .stack {{
      display: grid;
      gap: 18px;
    }}
    .panel {{
      border: 1px solid var(--border);
      border-radius: 24px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0.01)),
        var(--surface);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .panel-head {{
      padding: 16px 18px 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .panel-title {{
      margin: 0;
      font-size: 13px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--dim);
      font-family: var(--mono);
    }}
    .panel-body {{
      padding: 16px 18px 18px;
    }}
    .lede {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    button, select {{
      background: var(--surface-3);
      color: inherit;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 9px 13px;
      transition:
        border-color var(--dur) var(--ease),
        transform var(--dur) var(--ease),
        background var(--dur) var(--ease);
    }}
    button:hover, select:hover {{
      border-color: var(--border-strong);
      transform: translateY(-1px);
    }}
    .primary-btn {{
      background: linear-gradient(180deg, rgba(135, 199, 255, 0.24), rgba(74, 142, 255, 0.12));
      border-color: rgba(135, 199, 255, 0.45);
    }}
    input[type="range"] {{
      width: 100%;
      margin: 10px 0 14px;
      accent-color: var(--accent-strong);
    }}
    .frame-meta {{
      color: var(--dim);
      font-size: 12px;
      line-height: 1.5;
      margin-bottom: 14px;
      font-family: var(--mono);
    }}
    .frame-stage {{
      border: 1px solid var(--border);
      border-radius: 18px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01)),
        #090d14;
      overflow: hidden;
    }}
    .frame-stage-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      color: var(--dim);
      font-size: 12px;
      font-family: var(--mono);
    }}
    pre {{
      margin: 0;
      min-height: 620px;
      max-height: 68vh;
      overflow: auto;
      padding: 16px 18px 20px;
      background:
        radial-gradient(circle at top left, rgba(80, 138, 255, 0.08), transparent 18%),
        #090d14;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.13;
      font-size: 12px;
      font-family: var(--mono);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .stat {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 14px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.035), rgba(255, 255, 255, 0.01)),
        var(--surface-2);
    }}
    .stat strong {{
      display: block;
      font-size: 24px;
      line-height: 1;
      margin-bottom: 8px;
    }}
    .stat span {{
      color: var(--dim);
      font-size: 12px;
    }}
    .stat small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
      font-family: var(--mono);
    }}
    .chips {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 0;
    }}
    .chip {{
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.02);
      color: var(--dim);
      border-radius: 999px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 12px;
      font-family: var(--mono);
    }}
    .chip.active {{
      background: var(--accent-soft);
      border-color: rgba(135, 199, 255, 0.45);
      color: var(--text);
    }}
    .chip[data-tone="stream"], .event[data-tone="stream"] {{
      border-color: rgba(135, 199, 255, 0.34);
    }}
    .chip[data-tone="tool"], .event[data-tone="tool"] {{
      border-color: rgba(139, 226, 176, 0.34);
    }}
    .chip[data-tone="task"], .event[data-tone="task"] {{
      border-color: rgba(244, 191, 108, 0.34);
    }}
    .chip[data-tone="user"], .event[data-tone="user"] {{
      border-color: rgba(255, 139, 153, 0.3);
    }}
    .section-title {{
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
      margin: 0 0 12px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-family: var(--mono);
    }}
    .turn-list, .events {{
      display: grid;
      gap: 10px;
    }}
    .scroll-region {{
      max-height: 320px;
      overflow: auto;
      padding-right: 4px;
    }}
    .turn-card, .event {{
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 12px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.035), rgba(255, 255, 255, 0.015)),
        var(--surface-2);
      cursor: pointer;
      transition:
        border-color var(--dur) var(--ease),
        transform var(--dur) var(--ease),
        background var(--dur) var(--ease);
    }}
    .turn-card:hover, .event:hover {{
      transform: translateY(-1px);
      border-color: var(--border-strong);
    }}
    .turn-card.active, .event.active {{
      border-color: rgba(135, 199, 255, 0.55);
      background:
        linear-gradient(180deg, rgba(135, 199, 255, 0.12), rgba(135, 199, 255, 0.05)),
        var(--surface-2);
    }}
    .turn-card-head, .event-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 6px;
      font-family: var(--mono);
    }}
    .turn-card-head strong {{
      font-size: 13px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .turn-card-head span, .event-head span {{
      color: var(--muted);
      font-size: 11px;
    }}
    .turn-card-body, .event-body {{
      color: var(--dim);
      font-size: 12px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .json-key {{ color: #8cc5ff; }}
    .json-string {{ color: #d8e7ff; }}
    .json-number {{ color: #f1c57b; }}
    .json-boolean {{ color: #90dfb8; }}
    .json-null {{ color: #ff9cac; }}
    .turn-card-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
    }}
    .mini-pill {{
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.05);
      color: var(--muted);
      padding: 4px 7px;
      font-size: 10px;
      font-family: var(--mono);
    }}
    .artifact-list {{
      display: grid;
      gap: 8px;
    }}
    .artifact {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 10px 12px;
      background: rgba(255, 255, 255, 0.02);
    }}
    .artifact:hover {{
      border-color: var(--border-strong);
      background: rgba(255, 255, 255, 0.04);
    }}
    .artifact strong {{
      display: block;
      font-size: 12px;
      font-family: var(--mono);
      color: var(--text);
    }}
    .artifact span {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
    }}
    .artifact-tag {{
      color: var(--accent);
      font-size: 10px;
      font-family: var(--mono);
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .gallery {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .gallery a {{
      display: block;
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.02);
    }}
    .gallery a:hover {{
      border-color: var(--border-strong);
    }}
    .gallery img {{
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      background: #05070c;
    }}
    .gallery figcaption {{
      padding: 8px 10px 10px;
      font-size: 11px;
      color: var(--dim);
      font-family: var(--mono);
    }}
    .detail {{
      border: 1px solid var(--border);
      border-radius: 18px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01)),
        #09101a;
      overflow: hidden;
    }}
    .detail pre {{
      min-height: 0;
      max-height: 300px;
      border: 0;
      border-radius: 0;
      padding: 14px 16px 16px;
      font-size: 11px;
      background: transparent;
    }}
    .hint {{
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }}
    .empty {{
      padding: 14px;
      border: 1px dashed rgba(255, 255, 255, 0.12);
      border-radius: 16px;
      color: var(--muted);
      font-size: 12px;
      background: rgba(255, 255, 255, 0.02);
    }}
    @media (max-width: 1320px) {{
      .grid {{
        grid-template-columns: minmax(0, 1fr) 340px;
      }}
      .left-rail {{
        grid-column: 1 / -1;
      }}
    }}
    @media (max-width: 960px) {{
      .shell {{
        padding: 18px;
      }}
      .hero {{
        flex-direction: column;
        align-items: flex-start;
      }}
      .hero-meta {{
        justify-content: flex-start;
      }}
      .grid {{
        grid-template-columns: 1fr;
      }}
      pre {{
        min-height: 420px;
        max-height: 54vh;
      }}
      .gallery {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div>
        <div class="eyebrow">Successor Session Reviewer</div>
        <h1>{safe_title}</h1>
        <p>{safe_description}</p>
      </div>
      <div class="hero-meta">
        <div class="pill">Keyboard: Space play/pause</div>
        <div class="pill">Left/Right frame step</div>
        <div class="pill">Home/End jump</div>
      </div>
    </div>
    <div class="grid">
      <div class="stack left-rail">
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Bundle Summary</h2>
            <span class="pill">local-only artifact</span>
          </div>
          <div class="panel-body">
            <div class="stats">
              <div class="stat"><strong id="frameCount">0</strong><span>frames</span><small id="liveFrames">0 live / 0 tool</small></div>
              <div class="stat"><strong id="traceCount">0</strong><span>trace events</span><small id="eventTypes">0 event types</small></div>
              <div class="stat"><strong id="turnCount">0</strong><span>user turns</span><small id="maxAgentTurn">agent turn 0</small></div>
              <div class="stat"><strong id="duration">0.0s</strong><span>captured duration</span><small id="streamFrames">0 stream frames</small></div>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Artifacts</h2>
            <span class="lede" id="artifactMeta">bundle files</span>
          </div>
          <div class="panel-body">
            <div class="section-title">Primary files</div>
            <div id="artifactList" class="artifact-list"></div>
            <div class="section-title" style="margin-top:18px;">Turn files</div>
            <div id="turnFiles" class="artifact-list scroll-region"></div>
            <div id="artifactHint" class="hint" style="margin-top:12px;"></div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Visuals</h2>
            <span class="lede">screenshots and stills</span>
          </div>
          <div class="panel-body">
            <div id="gallery" class="gallery"></div>
          </div>
        </section>
      </div>
      <main class="stack">
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Playback</h2>
            <span class="pill" id="currentTurnPill">turn 0</span>
          </div>
          <div class="panel-body">
            <div class="toolbar">
              <button id="start">Start</button>
              <button id="prev">Prev</button>
              <button id="play" class="primary-btn">Play</button>
              <button id="next">Next</button>
              <button id="end">End</button>
              <label class="pill">speed
                <select id="speed">
                  <option value="0.25">0.25x</option>
                  <option value="0.5">0.5x</option>
                  <option value="1" selected>1x</option>
                  <option value="2">2x</option>
                  <option value="4">4x</option>
                  <option value="8">8x</option>
                </select>
              </label>
            </div>
            <input id="scrub" type="range" min="0" max="0" value="0">
            <div id="frameMeta" class="frame-meta"></div>
            <div class="frame-stage">
              <div class="frame-stage-head">
                <span id="frameStageLabel">Captured frame</span>
                <span id="frameStageCounters">frame 0/0</span>
              </div>
              <pre id="frameText">No frames captured.</pre>
            </div>
          </div>
        </section>
      </main>
      <div class="stack">
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Turns</h2>
            <span class="lede">jump into each user prompt window</span>
          </div>
          <div class="panel-body">
            <div id="turns" class="turn-list scroll-region"></div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Trace Explorer</h2>
            <span class="lede">filter and inspect nearby runtime events</span>
          </div>
          <div class="panel-body">
            <div id="eventFilters" class="chips" style="margin-bottom:12px;"></div>
            <div id="events" class="events scroll-region"></div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">Event Detail</h2>
            <span class="lede" id="detailLabel">select an event</span>
          </div>
          <div class="detail">
            <pre id="eventDetail">No event selected.</pre>
          </div>
        </section>
      </div>
    </div>
  </div>
  <script type="application/json" id="payload">{payload_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("payload").textContent);
    const frames = payload.frames || [];
    const traceEvents = payload.trace_events || [];
    const turnIds = payload.turns || [];
    const turnSummaries = payload.turn_summaries || [];
    const eventTypeCounts = payload.event_type_counts || {{}};
    const stats = payload.stats || {{}};
    const artifacts = payload.artifacts || {{}};
    const scrub = document.getElementById("scrub");
    const frameText = document.getElementById("frameText");
    const frameMeta = document.getElementById("frameMeta");
    const frameStageLabel = document.getElementById("frameStageLabel");
    const frameStageCounters = document.getElementById("frameStageCounters");
    const currentTurnPill = document.getElementById("currentTurnPill");
    const playBtn = document.getElementById("play");
    const speedSelect = document.getElementById("speed");
    const turnsBox = document.getElementById("turns");
    const eventFiltersBox = document.getElementById("eventFilters");
    const eventsBox = document.getElementById("events");
    const eventDetail = document.getElementById("eventDetail");
    const detailLabel = document.getElementById("detailLabel");
    const artifactList = document.getElementById("artifactList");
    const turnFiles = document.getElementById("turnFiles");
    const artifactMeta = document.getElementById("artifactMeta");
    const artifactHint = document.getElementById("artifactHint");
    const gallery = document.getElementById("gallery");
    let idx = 0;
    let timer = null;
    let activeEventKey = "";
    let activeEventType = "all";

    scrub.max = Math.max(0, frames.length - 1);
    document.getElementById("frameCount").textContent = String(stats.frame_count || frames.length);
    document.getElementById("traceCount").textContent = String(stats.trace_event_count || traceEvents.length);
    document.getElementById("turnCount").textContent = String(stats.turn_count || turnIds.length);
    document.getElementById("duration").textContent = Number(stats.duration_s || 0).toFixed(1) + "s";
    document.getElementById("liveFrames").textContent =
      String(stats.live_frame_count || 0) + " live / " + String(stats.tool_frame_count || 0) + " tool";
    document.getElementById("eventTypes").textContent =
      String(Object.keys(eventTypeCounts).length) + " event types";
    document.getElementById("maxAgentTurn").textContent =
      "agent turn " + String(stats.max_agent_turn || 0);
    document.getElementById("streamFrames").textContent =
      String(stats.stream_frame_count || 0) + " stream frames";
    artifactMeta.textContent =
      String((artifacts.primary || []).length) + " primary • " +
      String((artifacts.images || []).length) + " visuals";

    function formatDuration(value) {{
      return Number(value || 0).toFixed(1) + "s";
    }}

    function escapeHtml(text) {{
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }}

    function highlightJson(value, maxChars) {{
      let text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      if (maxChars && text.length > maxChars) {{
        text = text.slice(0, maxChars) + "…";
      }}
      let escaped = escapeHtml(text);
      escaped = escaped.replace(/&quot;((?:\\\\u[a-fA-F0-9]{{4}}|\\\\[^u]|[^\\\\&]|&(?!quot;))*?)&quot;(?=\\s*:)/g, '<span class="json-key">&quot;$1&quot;</span>');
      escaped = escaped.replace(/:\\s*&quot;((?:\\\\u[a-fA-F0-9]{{4}}|\\\\[^u]|[^\\\\&]|&(?!quot;))*?)&quot;/g, ': <span class="json-string">&quot;$1&quot;</span>');
      escaped = escaped.replace(/\\b(-?\\d+(?:\\.\\d+)?)\\b/g, '<span class="json-number">$1</span>');
      escaped = escaped.replace(/\\b(true|false)\\b/g, '<span class="json-boolean">$1</span>');
      escaped = escaped.replace(/\\bnull\\b/g, '<span class="json-null">null</span>');
      return escaped;
    }}

    function eventTone(name) {{
      const text = String(name || "").toLowerCase();
      if (text.startsWith("stream") || text.startsWith("continuation")) return "stream";
      if (text.startsWith("tool") || text.startsWith("bash")) return "tool";
      if (text.startsWith("task") || text.startsWith("subagent")) return "task";
      if (text.startsWith("user")) return "user";
      return "event";
    }}

    function nearestFrameIndex(targetS) {{
      if (!frames.length) return 0;
      let bestIdx = 0;
      let bestDelta = Number.POSITIVE_INFINITY;
      for (let i = 0; i < frames.length; i += 1) {{
        const frameT = Number(frames[i].scenario_elapsed_s || 0);
        const delta = Math.abs(frameT - targetS);
        if (delta < bestDelta) {{
          bestDelta = delta;
          bestIdx = i;
        }}
      }}
      return bestIdx;
    }}

    function selectedEventObject() {{
      if (!activeEventKey) return null;
      return traceEvents.find((ev) => eventKey(ev) === activeEventKey) || null;
    }}

    function renderArtifactList(container, items, emptyText) {{
      container.innerHTML = "";
      if (!items.length) {{
        container.innerHTML = '<div class="empty">' + emptyText + '</div>';
        return;
      }}
      for (const item of items) {{
        const link = document.createElement("a");
        link.className = "artifact";
        link.href = item.href;
        link.target = "_blank";

        const left = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = item.name;
        const sub = document.createElement("span");
        sub.textContent =
          item.href + " • " + Math.max(1, Math.round(Number(item.size_bytes || 0) / 1024)) + " KB";
        left.appendChild(title);
        left.appendChild(sub);

        const tag = document.createElement("span");
        tag.className = "artifact-tag";
        tag.textContent = String(item.kind || "file");

        link.appendChild(left);
        link.appendChild(tag);
        container.appendChild(link);
      }}
    }}

    function renderGallery() {{
      const images = artifacts.images || [];
      gallery.innerHTML = "";
      if (!images.length) {{
        gallery.innerHTML = '<div class="empty">No still images were captured in this bundle.</div>';
        return;
      }}
      for (const image of images) {{
        const link = document.createElement("a");
        link.href = image.href;
        link.target = "_blank";
        const figure = document.createElement("figure");
        figure.style.margin = "0";
        const img = document.createElement("img");
        img.src = image.href;
        img.alt = image.name;
        img.loading = "lazy";
        const caption = document.createElement("figcaption");
        caption.textContent = image.name;
        figure.appendChild(img);
        figure.appendChild(caption);
        link.appendChild(figure);
        gallery.appendChild(link);
      }}
      const hidden = artifacts.hidden_counts || {{}};
      if ((hidden.omitted_images || 0) > 0) {{
        const note = document.createElement("div");
        note.className = "hint";
        note.style.gridColumn = "1 / -1";
        note.textContent =
          String(hidden.omitted_images || 0) + " additional images omitted for readability.";
        gallery.appendChild(note);
      }}
    }}

    function eventKey(ev) {{
      return String(ev._source || "") + ":" + String(ev._line || "");
    }}

    function renderTurns(activeTurn) {{
      turnsBox.innerHTML = "";
      if (!turnSummaries.length) {{
        turnsBox.innerHTML = '<div class="empty">No user turns detected in this capture.</div>';
        return;
      }}
      for (const turn of turnSummaries) {{
        const card = document.createElement("div");
        card.className = "turn-card" + (Number(turn.turn_index || 0) === activeTurn ? " active" : "");
        card.addEventListener("click", () => {{
          stopPlayback();
          renderFrame(Number(turn.first_frame_idx || 0));
        }});
        const head = document.createElement("div");
        head.className = "turn-card-head";
        head.innerHTML =
          "<strong>turn " + String(turn.turn_index || 0) + "</strong>" +
          "<span>" + formatDuration(turn.duration_s || 0) + "</span>";
        const body = document.createElement("div");
        body.className = "turn-card-body";
        body.textContent =
          String(turn.frame_count || 0) + " frames • " +
          String(turn.live_frames || 0) + " live • " +
          String(turn.tool_frames || 0) + " tool-active";
        const meta = document.createElement("div");
        meta.className = "turn-card-meta";
        const chips = [
          "agent " + String(turn.max_agent_turn || 0),
          "messages " + String(turn.message_count_end || 0),
          "start " + formatDuration(turn.start_s || 0),
        ];
        if (Number(turn.stream_frames || 0) > 0) {{
          chips.push(String(turn.stream_frames || 0) + " stream");
        }}
        if (Number(turn.max_running_tools || 0) > 0) {{
          chips.push("peak tools " + String(turn.max_running_tools || 0));
        }}
        for (const label of chips) {{
          const chip = document.createElement("span");
          chip.className = "mini-pill";
          chip.textContent = label;
          meta.appendChild(chip);
        }}
        card.appendChild(head);
        card.appendChild(body);
        card.appendChild(meta);
        turnsBox.appendChild(card);
      }}
    }}

    function renderEventFilters() {{
      eventFiltersBox.innerHTML = "";
      const types = [["all", traceEvents.length], ...Object.entries(eventTypeCounts).sort((a, b) => a[0].localeCompare(b[0]))];
      for (const [name, count] of types) {{
        const btn = document.createElement("button");
        btn.className = "chip" + (name === activeEventType ? " active" : "");
        btn.dataset.tone = eventTone(name);
        btn.textContent = String(name) + " (" + String(count) + ")";
        btn.addEventListener("click", () => {{
          activeEventType = String(name);
          renderFrame(idx);
        }});
        eventFiltersBox.appendChild(btn);
      }}
    }}

    function renderEventDetail() {{
      const ev = selectedEventObject();
      if (!ev) {{
        detailLabel.textContent = "select an event";
        eventDetail.textContent = "No event selected.";
        return;
      }}
      detailLabel.textContent =
        String(ev.type || "event") + " @ " + Number(ev.t || 0).toFixed(3) + "s";
      const copy = Object.assign({{}}, ev);
      delete copy._source;
      delete copy._line;
      eventDetail.innerHTML = highlightJson(copy);
    }}

    function renderEvents(frame) {{
      const now = Number(frame.scenario_elapsed_s || 0);
      let relevant = traceEvents.filter(ev => {{
        const t = Number(ev.t || 0);
        const matchesType = activeEventType === "all" || String(ev.type || "event") === activeEventType;
        return matchesType && t >= Math.max(0, now - 5.0) && t <= now + 0.15;
      }}).slice(-28);
      if (!relevant.length && activeEventType !== "all") {{
        relevant = traceEvents.filter(
          (ev) => String(ev.type || "event") === activeEventType
        ).slice(-28);
      }}
      if (!relevant.length) {{
        eventsBox.innerHTML = '<div class="empty">No trace events match the current filter near this frame.</div>';
        activeEventKey = "";
        renderEventDetail();
        return;
      }}
      if (!relevant.some((ev) => eventKey(ev) === activeEventKey)) {{
        activeEventKey = eventKey(relevant[0]);
      }}
      eventsBox.innerHTML = "";
      for (const ev of relevant) {{
        const item = document.createElement("div");
        const key = eventKey(ev);
        item.className = "event" + (key === activeEventKey ? " active" : "");
        item.dataset.tone = eventTone(ev.type || "event");
        item.addEventListener("click", () => {{
          activeEventKey = key;
          stopPlayback();
          renderFrame(nearestFrameIndex(Number(ev.t || 0)));
        }});
        const head = document.createElement("div");
        head.className = "event-head";
        const name = document.createElement("strong");
        name.textContent = String(ev.type || "event");
        const when = document.createElement("span");
        when.textContent = Number(ev.t || 0).toFixed(3) + "s";
        head.appendChild(name);
        head.appendChild(when);
        const body = document.createElement("div");
        const copy = Object.assign({{}}, ev);
        delete copy._source;
        delete copy._line;
        body.className = "event-body";
        body.innerHTML = highlightJson(copy, 420);
        item.appendChild(head);
        item.appendChild(body);
        eventsBox.appendChild(item);
      }}
      renderEventDetail();
    }}

    function renderFrame(nextIdx) {{
      if (!frames.length) {{
        frameText.textContent = "No frames captured.";
        frameMeta.textContent = "";
        turnsBox.innerHTML = '<div class="empty">No turn markers available.</div>';
        eventsBox.innerHTML = '<div class="empty">No trace events loaded.</div>';
        frameStageLabel.textContent = "No frame data";
        frameStageCounters.textContent = "frame 0/0";
        currentTurnPill.textContent = "turn 0";
        return;
      }}
      idx = Math.max(0, Math.min(nextIdx, frames.length - 1));
      scrub.value = idx;
      const frame = frames[idx];
      frameText.textContent = frame.plain || "";
      frameStageLabel.textContent = String(frame.kind || "frame") + " frame";
      frameStageCounters.textContent =
        "frame " + String(frame.index || (idx + 1)) + "/" + String(frames.length);
      currentTurnPill.textContent = "turn " + String(frame.turn_index || 0);
      const parts = [
        "frame " + String(frame.index || (idx + 1)) + "/" + String(frames.length),
        "turn " + String(frame.turn_index || 0),
        String(frame.kind || "frame"),
        "elapsed " + Number(frame.scenario_elapsed_s || 0).toFixed(3) + "s",
        "turn " + Number(frame.turn_elapsed_s || 0).toFixed(3) + "s",
        "agent_turn=" + String(frame.agent_turn || 0),
        "stream=" + String(Boolean(frame.stream_open)),
        "running_tools=" + String(frame.running_tools || 0),
        "messages=" + String(frame.message_count || 0),
      ];
      frameMeta.textContent = parts.join(" | ");
      renderTurns(Number(frame.turn_index || 0));
      renderEvents(frame);
    }}

    function stopPlayback() {{
      if (timer !== null) {{
        clearTimeout(timer);
        timer = null;
      }}
      playBtn.textContent = "Play";
    }}

    function scheduleNextStep() {{
      if (idx >= frames.length - 1) {{
        stopPlayback();
        return;
      }}
      const current = Number(frames[idx].scenario_elapsed_s || 0);
      const next = Number(frames[idx + 1].scenario_elapsed_s || current);
      const speed = Number(speedSelect.value || 1);
      const delayMs = Math.max(20, ((next - current) * 1000) / Math.max(0.01, speed));
      timer = window.setTimeout(() => {{
        renderFrame(idx + 1);
        scheduleNextStep();
      }}, delayMs);
    }}

    document.getElementById("start").addEventListener("click", () => {{
      stopPlayback();
      renderFrame(0);
    }});
    document.getElementById("prev").addEventListener("click", () => {{
      stopPlayback();
      renderFrame(idx - 1);
    }});
    document.getElementById("next").addEventListener("click", () => {{
      stopPlayback();
      renderFrame(idx + 1);
    }});
    document.getElementById("end").addEventListener("click", () => {{
      stopPlayback();
      renderFrame(frames.length - 1);
    }});
    scrub.addEventListener("input", () => {{
      stopPlayback();
      renderFrame(Number(scrub.value));
    }});
    playBtn.addEventListener("click", () => {{
      if (timer !== null) {{
        stopPlayback();
        return;
      }}
      playBtn.textContent = "Pause";
      scheduleNextStep();
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.target && /input|select|textarea/i.test(event.target.tagName || "")) {{
        return;
      }}
      if (event.key === " ") {{
        event.preventDefault();
        playBtn.click();
      }} else if (event.key === "ArrowLeft") {{
        event.preventDefault();
        stopPlayback();
        renderFrame(idx - 1);
      }} else if (event.key === "ArrowRight") {{
        event.preventDefault();
        stopPlayback();
        renderFrame(idx + 1);
      }} else if (event.key === "Home") {{
        event.preventDefault();
        stopPlayback();
        renderFrame(0);
      }} else if (event.key === "End") {{
        event.preventDefault();
        stopPlayback();
        renderFrame(frames.length - 1);
      }}
    }});

    renderArtifactList(
      artifactList,
      artifacts.primary || [],
      "No primary artifact files were discovered next to this reviewer."
    );
    renderArtifactList(
      turnFiles,
      artifacts.turn_files || [],
      "No per-turn exports were found for this bundle."
    );
    const hidden = artifacts.hidden_counts || {{}};
    artifactHint.textContent =
      (hidden.hidden_stream_dirs || 0) > 0 || (hidden.hidden_stream_frames || 0) > 0 || (hidden.omitted_turn_files || 0) > 0
        ? String(hidden.hidden_stream_dirs || 0) + " turn-stream directories, " +
          String(hidden.hidden_stream_frames || 0) + " raw stream frames, and " +
          String(hidden.omitted_turn_files || 0) + " additional turn files hidden from the main index."
        : "Agent-friendly read order: summary.json → session_trace.json → timeline.json.";
    renderGallery();
    renderEventFilters();
    renderFrame(0);
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path


def write_recording_index(
    root: str | Path,
    *,
    title: str,
    description: str,
    frame_count: int,
    trace_event_count: int,
    duration_s: float,
) -> Path:
    """Human-readable artifact summary for a recording bundle."""
    root_path = Path(root)
    lines = [
        f"# {title}",
        "",
        description,
        "",
        f"Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Local-only debugging artifact bundle.",
        "",
        "## Summary",
        "",
        f"- frames: {frame_count}",
        f"- trace events: {trace_event_count}",
        f"- duration: {duration_s:.3f}s",
        "",
        "## Files",
        "",
        "- `input.jsonl` — raw input bytes with original timing",
        "- `timeline.json` — rendered frame timeline",
        "- `session_trace.jsonl` — raw runtime trace copied from the session log",
        "- `session_trace.json` — parsed trace events",
        "- `summary.json` — machine-readable bundle summary for agents/tools",
        "- `playback.html` — self-contained browser session reviewer",
        "",
        "## Read Order",
        "",
        "- agents: `summary.json` → `session_trace.json` → `timeline.json`",
        "- humans: `playback.html` first, then `index.md` / `session_trace.json`",
        "",
        "## Open It",
        "",
        "- `successor playback <bundle> --open`",
        "- `successor review <bundle> --open`",
        "- or open `playback.html` directly in a browser",
    ]
    path = root_path / "index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class RecordingBundle:
    """Capture a normal chat session into a portable playback bundle."""

    def __init__(
        self,
        root: str | Path,
        *,
        title: str = "Successor session playback",
        description: str = "Recorded via `successor record`.",
        frame_interval_s: float = 0.15,
    ) -> None:
        self.root = Path(root)
        self.title = title
        self.description = description
        self.frame_interval_s = max(0.01, frame_interval_s)
        self.input_path = self.root / "input.jsonl"
        self.timeline_path = self.root / "timeline.json"
        self.trace_jsonl_path = self.root / "session_trace.jsonl"
        self.trace_json_path = self.root / "session_trace.json"
        self.summary_path = self.root / "summary.json"
        self.viewer_path = self.root / "playback.html"
        self.index_path = self.root / "index.md"
        self._recorder = Recorder(self.input_path)
        self._t0 = 0.0
        self._last_capture_t = -1.0
        self._turn_started_at = 0.0
        self._current_turn_index = 0
        self._last_signature: tuple[object, ...] | None = None
        self._frames: list[dict[str, object]] = []

    def __enter__(self) -> "RecordingBundle":
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_bundle_is_gitignored(self.root)
        self._t0 = time.monotonic()
        self._last_capture_t = -1.0
        self._turn_started_at = 0.0
        self._current_turn_index = 0
        self._last_signature = None
        self._frames = []
        self._recorder.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._recorder.__exit__(exc_type, exc, tb)

    def record_byte(self, b: int) -> None:
        self._recorder.record_byte(b)

    def capture_frame(self, grid: Grid, *, chat: Any, force: bool = False) -> None:
        """Capture a rendered frame from a live chat tick."""
        if self._recorder._fp is None:
            return
        now_s = time.monotonic() - self._t0
        if not force and self._last_capture_t >= 0.0:
            if now_s - self._last_capture_t < self.frame_interval_s:
                return

        turn_index = sum(
            1
            for msg in getattr(chat, "messages", ())
            if getattr(msg, "role", None) == "user"
        )
        if turn_index != self._current_turn_index:
            self._current_turn_index = turn_index
            self._turn_started_at = now_s

        plain = render_grid_to_plain(grid)
        stream_open = bool(getattr(chat, "_stream", None) is not None)
        running_tools = len(getattr(chat, "_running_tools", ()))
        message_count = len(getattr(chat, "messages", ()))
        agent_turn = int(getattr(chat, "_agent_turn", 0))
        signature = (
            plain,
            turn_index,
            stream_open,
            running_tools,
            message_count,
            agent_turn,
        )
        if not force and signature == self._last_signature:
            return

        self._last_capture_t = now_s
        self._last_signature = signature
        self._frames.append(
            {
                "index": len(self._frames) + 1,
                "turn_index": turn_index,
                "kind": "live" if (stream_open or running_tools) else "idle",
                "frame_index": len(self._frames),
                "turn_elapsed_s": round(max(0.0, now_s - self._turn_started_at), 4),
                "scenario_elapsed_s": round(now_s, 4),
                "agent_turn": agent_turn,
                "stream_open": stream_open,
                "running_tools": running_tools,
                "message_count": message_count,
                "plain": plain,
            }
        )

    def finalize(self, *, trace_path: str | Path | None = None) -> dict[str, object]:
        """Write bundle artifacts and return a summary payload."""
        trace_events: list[dict[str, object]] = []
        if trace_path is not None:
            trace_file = Path(trace_path)
            if trace_file.exists():
                try:
                    shutil.copyfile(trace_file, self.trace_jsonl_path)
                except OSError:
                    pass
                trace_events = load_trace_events(trace_file)
        _write_json(self.timeline_path, self._frames)
        _write_json(self.trace_json_path, trace_events)
        write_playback_html(
            self.viewer_path,
            title=self.title,
            description=self.description,
            frames=self._frames,
            trace_events=trace_events,
            bundle_root=self.root,
        )
        duration_s = 0.0
        if self._frames:
            duration_s = float(self._frames[-1].get("scenario_elapsed_s", 0.0) or 0.0)
        summary = {
            "title": self.title,
            "description": self.description,
            "local_only": True,
            "frame_count": len(self._frames),
            "trace_event_count": len(trace_events),
            "duration_s": round(duration_s, 4),
            "input_path": str(self.input_path),
            "timeline_path": str(self.timeline_path),
            "trace_jsonl_path": str(self.trace_jsonl_path),
            "trace_json_path": str(self.trace_json_path),
            "viewer_path": str(self.viewer_path),
            "index_path": str(self.index_path),
            "recommended_read_order": [
                str(self.summary_path),
                str(self.trace_json_path),
                str(self.timeline_path),
            ],
        }
        _write_json(self.summary_path, summary)
        write_recording_index(
            self.root,
            title=self.title,
            description=self.description,
            frame_count=len(self._frames),
            trace_event_count=len(trace_events),
            duration_s=duration_s,
        )
        return summary


def bundle_path_from_input(value: str | None) -> Path:
    """Resolve a CLI recording output argument into a concrete path."""
    if value is None or not value.strip():
        return default_recording_bundle_dir()
    return Path(value)


def is_bundle_path(path: str | Path) -> bool:
    """Whether an output path should be treated as a recording bundle dir."""
    value = Path(path)
    if value.exists() and value.is_dir():
        return True
    return value.suffix.lower() != ".jsonl"


def nearest_frame_index_for_time(
    frames: list[dict[str, object]],
    target_s: float,
) -> int:
    """Public helper for tests and tooling."""
    return _closest_frame_index(frames, target_s)
