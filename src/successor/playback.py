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
from pathlib import Path
from typing import Any

from .recorder import Recorder
from .reviewer import theme_catalog_payload, viewer_defaults, write_reviewer_html
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


def recordings_library_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else recordings_dir()
    return base / "recordings.html"


def _library_href_for_bundle(bundle_root: str | Path | None) -> str | None:
    if bundle_root is None:
        return None
    bundle_path = Path(bundle_root)
    library_path = recordings_library_path()
    try:
        rel = library_path.relative_to(bundle_path)
    except ValueError:
        try:
            rel = Path(os.path.relpath(library_path, bundle_path))
        except ValueError:
            return None
    return rel.as_posix()


def _bundle_event_types(trace_events: list[dict[str, object]]) -> list[str]:
    names = {str(event.get("type") or "event") for event in trace_events}
    return sorted(name for name in names if name)


def _bundle_tools(trace_events: list[dict[str, object]]) -> list[str]:
    tools: set[str] = set()
    for event in trace_events:
        event_type = str(event.get("type") or "")
        route = str(event.get("route") or "")
        tool_name = str(event.get("tool") or "")
        verb = str(event.get("verb") or "")
        if "browser" in event_type or route.startswith("browser"):
            tools.add("browser")
        if "vision" in event_type or route.startswith("vision"):
            tools.add("vision")
        if "holonet" in event_type or route.startswith("holonet"):
            tools.add("holonet")
        if "subagent" in event_type or tool_name == "subagent":
            tools.add("subagent")
        if "task" in event_type or tool_name == "task":
            tools.add("task")
        if "bash" in event_type or tool_name == "bash" or verb.startswith("bash"):
            tools.add("bash")
    return sorted(tools)


def _bundle_status(
    *,
    trace_events: list[dict[str, object]],
    images_count: int,
    frame_count: int,
    turn_count: int,
) -> tuple[str, str]:
    event_types = _bundle_event_types(trace_events)
    if any("error" in event or "cancel" in event or "fail" in event for event in event_types):
        return "Needs Review", "Run emitted failure or cancellation signals."
    if images_count > 0 and turn_count >= 2 and frame_count >= 12:
        return "Showcase", "Captured enough evidence and stills to be promo-friendly."
    if any("browser" in event or "vision" in event or "subagent" in event for event in event_types):
        return "Interesting", "Run exercised higher-value tools worth reviewing."
    if turn_count <= 1:
        return "Fresh", "Short run with minimal structure, likely a quick probe."
    return "Clean", "No obvious failure signals; suitable for normal archival."


def _bundle_preview_excerpt(frames: list[dict[str, object]]) -> str:
    if not frames:
        return ""
    chosen = (
        next(
            (
                frame
                for frame in reversed(frames)
                if bool(frame.get("stream_open"))
                or _as_int(frame.get("running_tools", 0)) > 0
                or _as_int(frame.get("turn_index", 0)) > 0
            ),
            None,
        )
        or frames[-1]
    )
    text = str(chosen.get("plain") or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    excerpt = "\n".join(lines[:7])
    if len(lines) > 7:
        excerpt += "\n…"
    return excerpt


def _bundle_identity(frames: list[dict[str, object]], bundle_root: Path) -> str:
    label_map = {"profile": "", "provider": "", "appearance": ""}
    for frame in frames[:3]:
        text = str(frame.get("plain") or "")
        if not text:
            continue
        lines = [line.rstrip() for line in text.splitlines()]
        for idx, raw in enumerate(lines):
            line = raw.strip().lower()
            if line in label_map and not label_map[line]:
                for next_line in lines[idx + 1 : idx + 4]:
                    cleaned = next_line.strip()
                    if cleaned:
                        label_map[line] = cleaned
                        break
    parts = [
        label_map["profile"],
        label_map["appearance"].split("·", 1)[0].strip() if label_map["appearance"] else "",
        label_map["provider"],
    ]
    identity = " · ".join(part for part in parts if part)
    return identity or bundle_root.name


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
    bundle_path = Path(bundle_root) if bundle_root is not None else None
    if bundle_path is not None and title.strip().lower() == "successor session playback":
        title = _bundle_identity(frames, bundle_path)
    if not description.strip() or description == "Recorded via `successor record`.":
        description = "Auto-recorded local session bundle."
    default_theme, default_mode = viewer_defaults()
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
        "kind": "session",
        "title": title,
        "description": description,
        "frames": frames,
        "trace_events": trace_events,
        "turns": turns,
        "turn_summaries": turn_summaries,
        "event_type_counts": event_type_counts,
        "stats": stats,
        "artifacts": _discover_bundle_artifacts(bundle_root),
        "theme_catalog": theme_catalog_payload(),
        "default_theme": default_theme,
        "default_mode": default_mode,
        "library_href": _library_href_for_bundle(bundle_root),
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
    """Write a frontend-backed session reviewer for recorded bundles."""
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
    return write_reviewer_html(output_path, payload, title=title)


def _bundle_summary_for_library(bundle_root: Path) -> dict[str, object] | None:
    timeline_path = bundle_root / "timeline.json"
    summary_path = bundle_root / "summary.json"
    trace_json_path = bundle_root / "session_trace.json"
    trace_jsonl_path = bundle_root / "session_trace.jsonl"
    if not summary_path.exists() or not timeline_path.exists():
        return None

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(summary, dict):
        return None

    try:
        frames = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        frames = []
    if not isinstance(frames, list):
        frames = []

    if trace_json_path.exists():
        try:
            trace_events = json.loads(trace_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            trace_events = []
    else:
        trace_events = load_trace_events(trace_jsonl_path)
    if not isinstance(trace_events, list):
        trace_events = []

    turn_summaries = _build_turn_summaries(frames)
    images = _discover_bundle_artifacts(bundle_root).get("images", [])
    title = str(summary.get("title") or bundle_root.name)
    if title.strip().lower() == "successor session playback":
        title = _bundle_identity(frames, bundle_root)
    description = str(summary.get("description") or "").strip()
    if not description or description == "Recorded via `successor record`.":
        description = "Auto-recorded local session bundle."
    status, status_reason = _bundle_status(
        trace_events=trace_events,
        images_count=len(images) if isinstance(images, list) else 0,
        frame_count=len(frames),
        turn_count=len(turn_summaries),
    )
    viewer_path = bundle_root / "playback.html"
    index_path = bundle_root / "index.md"
    updated_at = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.localtime(bundle_root.stat().st_mtime),
    )
    return {
        "slug": bundle_root.name,
        "title": title,
        "description": description,
        "href": f"{bundle_root.name}/{viewer_path.name}",
        "index_href": f"{bundle_root.name}/{index_path.name}" if index_path.exists() else None,
        "status": status,
        "status_reason": status_reason,
        "updated_at": updated_at,
        "duration_s": round(float(summary.get("duration_s", 0.0) or 0.0), 4),
        "frame_count": int(summary.get("frame_count", 0) or 0),
        "trace_event_count": int(summary.get("trace_event_count", 0) or 0),
        "turn_count": len(turn_summaries),
        "max_agent_turn": max(
            (_as_int(turn.get("max_agent_turn", 0)) for turn in turn_summaries),
            default=0,
        ),
        "visuals_count": len(images) if isinstance(images, list) else 0,
        "preview_excerpt": _bundle_preview_excerpt(frames),
        "tools": _bundle_tools(trace_events),
        "event_types": _bundle_event_types(trace_events),
    }


def _refresh_bundle_viewer(bundle_root: Path) -> None:
    timeline_path = bundle_root / "timeline.json"
    if not timeline_path.exists():
        return
    summary_path = bundle_root / "summary.json"
    trace_json_path = bundle_root / "session_trace.json"
    trace_jsonl_path = bundle_root / "session_trace.jsonl"
    try:
        frames = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(frames, list):
        return
    title = "Successor session playback"
    description = "Recorded via `successor record`."
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        if isinstance(summary, dict):
            title = str(summary.get("title") or title)
            description = str(summary.get("description") or description)
    if trace_json_path.exists():
        try:
            trace_events = json.loads(trace_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            trace_events = []
    else:
        trace_events = load_trace_events(trace_jsonl_path)
    if not isinstance(trace_events, list):
        trace_events = []
    write_playback_html(
        bundle_root / "playback.html",
        title=title,
        description=description,
        frames=frames,
        trace_events=trace_events,
        bundle_root=bundle_root,
    )


def build_recordings_library_payload(root: str | Path) -> dict[str, object]:
    default_theme, default_mode = viewer_defaults()
    root_path = Path(root)
    sessions: list[dict[str, object]] = []
    if root_path.exists():
        for child in sorted(root_path.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if not child.is_dir():
                continue
            summary = _bundle_summary_for_library(child)
            if summary is not None:
                sessions.append(summary)
    return {
        "kind": "library",
        "title": "Successor recordings",
        "description": "Board and list views for accumulated local session bundles. Open a card to move into the full review surface.",
        "sessions": sessions,
        "theme_catalog": theme_catalog_payload(),
        "default_theme": default_theme,
        "default_mode": default_mode,
    }


def write_recordings_html(root: str | Path) -> Path:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    if root_path.exists():
        for child in root_path.iterdir():
            if child.is_dir():
                _refresh_bundle_viewer(child)
    payload = build_recordings_library_payload(root_path)
    return write_reviewer_html(
        recordings_library_path(root_path),
        payload,
        title="Successor recordings",
    )


def prepare_recording_viewer(
    requested: str | Path | None = None,
    *,
    library: bool = False,
) -> tuple[Path, Path, bool]:
    """Resolve and regenerate a recordings manager or bundle reviewer.

    Returns `(viewer_path, bundle_root, is_library)`.
    Raises `FileNotFoundError` when there is nothing suitable to open.
    """
    raw = ""
    if isinstance(requested, Path):
        raw = str(requested)
    elif isinstance(requested, str):
        raw = requested.strip()
    want_library = library or raw == "recordings"
    path = Path(raw) if raw and raw != "recordings" else (
        recordings_dir() if want_library else latest_recording_bundle_dir()
    )
    if path is None:
        raise FileNotFoundError("no recording bundles found")
    if not path.exists() and not want_library:
        raise FileNotFoundError(f"no such path: {path}")

    viewer_path: Path
    bundle_root: Path
    if want_library or (path.is_dir() and path.resolve() == recordings_dir().resolve()):
        bundle_root = path
        viewer_path = write_recordings_html(path)
        return viewer_path, bundle_root, True

    if path.is_file() and path.suffix.lower() == ".html":
        bundle_root = path.parent
        viewer_path = bundle_root / "playback.html"
    else:
        bundle_root = path if path.is_dir() else path.parent
        viewer_path = bundle_root / "playback.html"

    timeline_path = bundle_root / "timeline.json"
    if timeline_path.exists():
        _refresh_bundle_viewer(bundle_root)
    elif not viewer_path.exists():
        raise FileNotFoundError(
            "no playback reviewer found and no timeline.json to regenerate from"
        )
    return viewer_path, bundle_root, False


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
        return self.refresh_viewer(trace_path=trace_path, copy_trace_jsonl=True)

    def refresh_viewer(
        self,
        *,
        trace_path: str | Path | None = None,
        copy_trace_jsonl: bool = False,
    ) -> dict[str, object]:
        """Write the current live bundle state to disk and regenerate its viewer."""
        trace_events: list[dict[str, object]] = []
        if trace_path is not None:
            trace_file = Path(trace_path)
            if trace_file.exists():
                if copy_trace_jsonl:
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
        recordings_root = recordings_dir().resolve()
        try:
            if self.root.resolve().parent == recordings_root:
                write_recordings_html(recordings_root)
        except OSError:
            pass
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
