"""Hermetic tests for the background subagent manager."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from successor.profiles import Profile, SubagentConfig
from successor.providers.llama import ContentChunk, StreamEnded, StreamError, StreamStarted
from successor.subagents.manager import SubagentManager


class _StaticStream:
    def __init__(self, events: list[object]) -> None:
        self._events = list(events)
        self._closed = False
        self._reported_close = False

    def drain(self) -> list[object]:
        if self._closed and not self._reported_close:
            self._reported_close = True
            return [StreamError("cancelled")]
        if self._closed:
            return []
        if not self._events:
            return []
        events = list(self._events)
        self._events.clear()
        return events

    def close(self) -> None:
        self._closed = True


class _GateStream:
    def __init__(self, release_event, *, text: str) -> None:
        self._release_event = release_event
        self._text = text
        self._started = False
        self._done = False
        self._closed = False
        self._reported_close = False

    def drain(self) -> list[object]:
        if self._closed and not self._reported_close:
            self._reported_close = True
            return [StreamError("cancelled")]
        if self._closed:
            return []
        if not self._started:
            self._started = True
            return [StreamStarted()]
        if self._done or not self._release_event.is_set():
            return []
        self._done = True
        return [
            ContentChunk(text=self._text),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]

    def close(self) -> None:
        self._closed = True


@dataclass
class _MockClient:
    stream_factory: object
    base_url: str = "http://mock"
    model: str = "mock-model"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None, tools=None):
        return self.stream_factory()

    def health(self) -> bool:
        return True

    def detect_context_window(self) -> int:
        return 200_000


def _wait_until(predicate, *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


def test_manager_completes_task_and_writes_transcript(temp_config_dir: Path) -> None:
    manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=lambda profile: _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="child result"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        ),
        settle_sleep_s=0.01,
    )
    snapshot = manager.spawn_fork(
        directive="summarize the repo",
        context_snapshot=[],
        profile=Profile(name="subagent-test"),
        config=SubagentConfig(),
    )
    _wait_until(lambda: not manager.has_active_tasks())

    task = next(t for t in manager.snapshots() if t.task_id == snapshot.task_id)
    assert task.role == "worker"
    assert task.status == "completed"
    assert task.transcript_path.exists()
    payload = json.loads(task.transcript_path.read_text())
    assert payload["status"] == "completed"
    assert payload["result_excerpt"] == "child result"
    assert payload["result_text"] == "child result"
    notes = manager.drain_notifications()
    assert notes and notes[0].task.task_id == task.task_id


def test_manager_child_profile_strips_subagent_tool(temp_config_dir: Path) -> None:
    seen_tools: list[tuple[str, ...]] = []

    def client_factory(profile):
        seen_tools.append(tuple(profile.tools))
        return _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="child result"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        )

    manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=client_factory,
        settle_sleep_s=0.01,
    )
    manager.spawn_fork(
        directive="inspect",
        name="audit",
        context_snapshot=[],
        profile=Profile(name="subagent-test", tools=("bash", "subagent")),
        config=SubagentConfig(),
    )
    _wait_until(lambda: not manager.has_active_tasks())

    assert seen_tools == [("bash",)]


def test_manager_verification_role_strips_write_tools_and_mutating_bash(
    temp_config_dir: Path,
) -> None:
    seen_profiles: list[Profile] = []

    def client_factory(profile):
        seen_profiles.append(profile)
        return _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="Scope: verify\nChecks: pytest -q\nVerdict: PASS"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        )

    manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=client_factory,
        settle_sleep_s=0.01,
    )
    manager.spawn_fork(
        directive="verify the build",
        role="verification",
        context_snapshot=[],
        profile=Profile(
            name="subagent-test",
            tools=("bash", "read_file", "write_file", "edit_file", "subagent"),
        ),
        config=SubagentConfig(),
    )
    _wait_until(lambda: not manager.has_active_tasks())

    assert len(seen_profiles) == 1
    child_profile = seen_profiles[0]
    assert "subagent" not in child_profile.tools
    assert "write_file" not in child_profile.tools
    assert "edit_file" not in child_profile.tools
    assert "read_file" in child_profile.tools
    bash_cfg = child_profile.tool_config.get("bash", {})
    assert bash_cfg["allow_mutating"] is False
    assert bash_cfg["allow_dangerous"] is False


def test_manager_queues_second_task_when_capacity_is_one(temp_config_dir: Path) -> None:
    import threading

    release_first = threading.Event()
    call_count = {"n": 0}

    def client_factory(profile):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx == 0:
            return _MockClient(lambda: _GateStream(release_first, text="first done"))
        return _MockClient(lambda: _StaticStream([
            StreamStarted(),
            ContentChunk(text="second done"),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]))

    manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=client_factory,
        settle_sleep_s=0.01,
    )

    first = manager.spawn_fork(
        directive="first",
        context_snapshot=[],
        profile=Profile(name="queue-test"),
        config=SubagentConfig(),
    )
    second = manager.spawn_fork(
        directive="second",
        context_snapshot=[],
        profile=Profile(name="queue-test"),
        config=SubagentConfig(),
    )

    def _queued_state_visible() -> bool:
        snapshots = {task.task_id: task for task in manager.snapshots()}
        return (
            snapshots[first.task_id].status == "running"
            and snapshots[second.task_id].status == "queued"
        )

    _wait_until(_queued_state_visible)
    release_first.set()
    _wait_until(lambda: not manager.has_active_tasks())

    snapshots = {task.task_id: task for task in manager.snapshots()}
    assert snapshots[first.task_id].status == "completed"
    assert snapshots[second.task_id].status == "completed"
    assert snapshots[second.task_id].started_at is not None
    assert snapshots[first.task_id].finished_at is not None
    assert snapshots[second.task_id].started_at >= snapshots[first.task_id].started_at


def test_manager_can_cancel_queued_task(temp_config_dir: Path) -> None:
    import threading

    release_first = threading.Event()
    call_count = {"n": 0}

    def client_factory(profile):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx == 0:
            return _MockClient(lambda: _GateStream(release_first, text="first done"))
        return _MockClient(lambda: _StaticStream([
            StreamStarted(),
            ContentChunk(text="second done"),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]))

    manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=client_factory,
        settle_sleep_s=0.01,
    )

    first = manager.spawn_fork(
        directive="first",
        context_snapshot=[],
        profile=Profile(name="cancel-test"),
        config=SubagentConfig(),
    )
    second = manager.spawn_fork(
        directive="second",
        context_snapshot=[],
        profile=Profile(name="cancel-test"),
        config=SubagentConfig(),
    )

    _wait_until(
        lambda: {
            task.task_id: task.status for task in manager.snapshots()
        }.get(second.task_id) == "queued"
    )
    assert manager.cancel(second.task_id) == 1
    release_first.set()
    _wait_until(lambda: not manager.has_active_tasks())

    snapshots = {task.task_id: task for task in manager.snapshots()}
    assert snapshots[first.task_id].status == "completed"
    assert snapshots[second.task_id].status == "cancelled"


def test_manager_applies_deferred_reconfigure_once_idle(temp_config_dir: Path) -> None:
    import threading

    release_first = threading.Event()
    release_parallel = threading.Event()
    call_count = {"n": 0}

    def client_factory(profile):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx == 0:
            return _MockClient(lambda: _GateStream(release_first, text="first done"))
        if idx == 1:
            return _MockClient(lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="second done"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ]))
        return _MockClient(
            lambda: _GateStream(
                release_parallel,
                text=f"parallel {idx} done",
            )
        )

    manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=client_factory,
        settle_sleep_s=0.01,
    )

    first = manager.spawn_fork(
        directive="first",
        context_snapshot=[],
        profile=Profile(name="reconfigure-test"),
        config=SubagentConfig(),
    )
    second = manager.spawn_fork(
        directive="second",
        context_snapshot=[],
        profile=Profile(name="reconfigure-test"),
        config=SubagentConfig(),
    )

    _wait_until(
        lambda: {
            task.task_id: task.status for task in manager.snapshots()
        } == {
            first.task_id: "running",
            second.task_id: "queued",
        }
    )
    assert manager.reconfigure(max_model_tasks=2) is False

    release_first.set()
    _wait_until(lambda: not manager.has_active_tasks())

    third = manager.spawn_fork(
        directive="third",
        context_snapshot=[],
        profile=Profile(name="reconfigure-test"),
        config=SubagentConfig(),
    )
    fourth = manager.spawn_fork(
        directive="fourth",
        context_snapshot=[],
        profile=Profile(name="reconfigure-test"),
        config=SubagentConfig(),
    )

    def _parallel_running() -> bool:
        snapshots = {task.task_id: task for task in manager.snapshots()}
        return (
            snapshots[third.task_id].status == "running"
            and snapshots[fourth.task_id].status == "running"
        )

    _wait_until(_parallel_running)
    release_parallel.set()
    _wait_until(lambda: not manager.has_active_tasks())
