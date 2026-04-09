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


def write_playback_html(
    output: str | Path,
    *,
    title: str,
    description: str,
    frames: list[dict[str, object]],
    trace_events: list[dict[str, object]],
) -> Path:
    """Write a self-contained HTML scrubber for recorded frames."""
    output_path = Path(output)
    if output_path.suffix.lower() != ".html":
        output_path = output_path / "playback.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "title": title,
        "description": description,
        "frames": frames,
        "trace_events": trace_events,
        "turns": sorted({
            int(frame.get("turn_index", 0) or 0)
            for frame in frames
            if int(frame.get("turn_index", 0) or 0) > 0
        }),
    }
    payload_json = (
        json.dumps(payload)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0e1118;
      --panel: #151b26;
      --panel-2: #0b0f15;
      --text: #e8edf6;
      --muted: #9ca8bb;
      --accent: #63c7ff;
      --accent-2: #8ce9ff;
      --border: #253043;
      --chip: #1a2332;
      --chip-active: #1e3956;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top, #172031 0%, var(--bg) 55%);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .wrap {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 400px;
      min-height: 100vh;
    }}
    .main, .side {{
      padding: 18px;
    }}
    .side {{
      border-left: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.00)), var(--panel);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 22px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-bottom: 14px;
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}
    button, select {{
      background: #1b2433;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 7px 11px;
      font: inherit;
    }}
    button:hover, select:hover {{
      border-color: var(--accent);
    }}
    input[type="range"] {{
      width: 100%;
      margin: 10px 0 12px;
    }}
    .frame-meta {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-bottom: 12px;
    }}
    pre {{
      margin: 0;
      min-height: calc(100vh - 230px);
      max-height: calc(100vh - 230px);
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.00)), var(--panel-2);
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.12;
      font-size: 12px;
    }}
    .chips {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 10px 0 14px;
    }}
    .chip {{
      border: 1px solid var(--border);
      background: var(--chip);
      color: var(--muted);
      border-radius: 999px;
      padding: 4px 9px;
      cursor: pointer;
      font-size: 12px;
    }}
    .chip.active {{
      background: var(--chip-active);
      border-color: var(--accent);
      color: var(--text);
    }}
    .section-title {{
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
      margin: 16px 0 8px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 14px;
    }}
    .stat {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      background: rgba(255,255,255,0.02);
    }}
    .stat strong {{
      display: block;
      color: var(--text);
      font-size: 16px;
      margin-bottom: 2px;
    }}
    .stat span {{
      color: var(--muted);
      font-size: 11px;
    }}
    .events {{
      max-height: 44vh;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--panel-2);
      padding: 8px;
    }}
    .event {{
      border: 1px solid #182131;
      border-radius: 10px;
      padding: 8px;
      margin-bottom: 8px;
      background: rgba(255,255,255,0.02);
      cursor: pointer;
    }}
    .event:last-child {{
      margin-bottom: 0;
    }}
    .event.active {{
      border-color: var(--accent);
      background: rgba(99, 199, 255, 0.12);
    }}
    .event-head {{
      color: var(--accent-2);
      font-size: 12px;
      margin-bottom: 4px;
    }}
    .event-body {{
      color: var(--muted);
      font-size: 11px;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .hint {{
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="main">
      <h1>{title}</h1>
      <div class="meta">{description}</div>
      <div class="toolbar">
        <button id="start">Start</button>
        <button id="prev">Prev</button>
        <button id="play">Play</button>
        <button id="next">Next</button>
        <button id="end">End</button>
        <label class="meta" style="margin:0;">speed
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
      <pre id="frameText">No frames captured.</pre>
    </div>
    <div class="side">
      <div class="hint">
        Interactive playback scrubber. Open this file directly in a browser.
        Keyboard: Space play/pause, Left/Right step, Home/End jump.
      </div>
      <div class="section-title">Summary</div>
      <div class="stats">
        <div class="stat"><strong id="frameCount">0</strong><span>frames</span></div>
        <div class="stat"><strong id="traceCount">0</strong><span>trace events</span></div>
        <div class="stat"><strong id="turnCount">0</strong><span>user turns</span></div>
        <div class="stat"><strong id="duration">0.0s</strong><span>captured duration</span></div>
      </div>
      <div class="section-title">Turns</div>
      <div id="turns" class="chips"></div>
      <div class="section-title">Nearby Trace Events</div>
      <div id="events" class="events"></div>
    </div>
  </div>
  <script type="application/json" id="payload">{payload_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("payload").textContent);
    const frames = payload.frames || [];
    const traceEvents = payload.trace_events || [];
    const turnIds = payload.turns || [];
    const scrub = document.getElementById("scrub");
    const frameText = document.getElementById("frameText");
    const frameMeta = document.getElementById("frameMeta");
    const playBtn = document.getElementById("play");
    const speedSelect = document.getElementById("speed");
    const turnsBox = document.getElementById("turns");
    const eventsBox = document.getElementById("events");
    let idx = 0;
    let timer = null;
    let activeEventKey = "";

    scrub.max = Math.max(0, frames.length - 1);
    document.getElementById("frameCount").textContent = String(frames.length);
    document.getElementById("traceCount").textContent = String(traceEvents.length);
    document.getElementById("turnCount").textContent = String(turnIds.length);
    const duration = frames.length ? Number(frames[frames.length - 1].scenario_elapsed_s || 0).toFixed(1) : "0.0";
    document.getElementById("duration").textContent = duration + "s";

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

    function eventKey(ev) {{
      return String(ev._source || "") + ":" + String(ev._line || "");
    }}

    function renderTurns(activeTurn) {{
      turnsBox.innerHTML = "";
      if (!turnIds.length) {{
        turnsBox.innerHTML = '<div class="hint">No user turns detected in this capture.</div>';
        return;
      }}
      for (const turn of turnIds) {{
        const btn = document.createElement("button");
        btn.className = "chip" + (turn === activeTurn ? " active" : "");
        btn.textContent = "turn " + turn;
        btn.addEventListener("click", () => {{
          stopPlayback();
          const nextIdx = frames.findIndex(frame => Number(frame.turn_index || 0) === turn);
          renderFrame(nextIdx >= 0 ? nextIdx : 0);
        }});
        turnsBox.appendChild(btn);
      }}
    }}

    function renderEvents(frame) {{
      const now = Number(frame.scenario_elapsed_s || 0);
      const relevant = traceEvents.filter(ev => {{
        const t = Number(ev.t || 0);
        return t >= Math.max(0, now - 4.0) && t <= now + 0.05;
      }}).slice(-24);
      if (!relevant.length) {{
        eventsBox.innerHTML = '<div class="hint">No trace events near this frame.</div>';
        return;
      }}
      eventsBox.innerHTML = "";
      for (const ev of relevant) {{
        const item = document.createElement("div");
        const key = eventKey(ev);
        item.className = "event" + (key === activeEventKey ? " active" : "");
        item.addEventListener("click", () => {{
          activeEventKey = key;
          stopPlayback();
          renderFrame(nearestFrameIndex(Number(ev.t || 0)));
        }});
        const head = document.createElement("div");
        head.className = "event-head";
        head.textContent = Number(ev.t || 0).toFixed(3) + "s · " + String(ev.type || "event");
        const body = document.createElement("div");
        const copy = Object.assign({{}}, ev);
        delete copy._source;
        delete copy._line;
        body.className = "event-body";
        body.textContent = JSON.stringify(copy, null, 2);
        item.appendChild(head);
        item.appendChild(body);
        eventsBox.appendChild(item);
      }}
    }}

    function renderFrame(nextIdx) {{
      if (!frames.length) {{
        frameText.textContent = "No frames captured.";
        frameMeta.textContent = "";
        turnsBox.innerHTML = '<div class="hint">No turn markers available.</div>';
        eventsBox.innerHTML = '<div class="hint">No trace events loaded.</div>';
        return;
      }}
      idx = Math.max(0, Math.min(nextIdx, frames.length - 1));
      scrub.value = idx;
      const frame = frames[idx];
      frameText.textContent = frame.plain || "";
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
        "- `playback.html` — self-contained browser scrubber",
        "",
        "## Read Order",
        "",
        "- agents: `summary.json` → `session_trace.json` → `timeline.json`",
        "- humans: `playback.html` first, then `index.md` / `session_trace.json`",
        "",
        "## Open It",
        "",
        "- `successor playback <bundle> --open`",
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
