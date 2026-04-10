"""Background subagent task manager.

The first production version intentionally reuses the existing chat
runtime instead of inventing a second agent loop. Each child task runs
inside a headless `SuccessorChat`, which means it inherits the real
tool-calling behavior, continuation loop, compaction gate, and provider
client behavior the user already relies on in the foreground chat.

This buys us:

  - isolated transcripts per task
  - queueing / cancellation
  - real tool execution and multi-turn continuation
  - live local-model verification against the same runtime path

The scheduler is deliberately simple for the foundation phase:
`max_model_tasks` is a semaphore over background child chats. The
foreground chat is not yet routed through the same semaphore.
Queue-width edits made while tasks are active are deferred until the
manager goes idle, so persisted profile changes eventually take effect
without mutating the live semaphore mid-flight.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ..loader import config_dir
from ..profiles import Profile
from .config import SubagentConfig
from .prompt import build_child_prompt, normalize_subagent_role


def _default_transcript_dir() -> Path:
    return config_dir() / "subagents"


def _default_client_factory(profile: Profile) -> object:
    from ..providers import make_provider
    from ..providers.llama import LlamaCppClient

    if profile.provider:
        try:
            return make_provider(profile.provider)
        except Exception:
            return LlamaCppClient()
    return LlamaCppClient()


def _now_ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _excerpt(text: str, limit: int = 140) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _serialize_message(msg: object) -> dict[str, Any]:
    tool_card = getattr(msg, "tool_card", None)
    subagent_card = getattr(msg, "subagent_card", None)
    payload: dict[str, Any] = {
        "role": getattr(msg, "role", ""),
        "content": getattr(msg, "raw_text", ""),
        "display_text": getattr(msg, "display_text", ""),
        "synthetic": bool(getattr(msg, "synthetic", False)),
        "is_summary": bool(getattr(msg, "is_summary", False)),
        "api_role_override": getattr(msg, "api_role_override", None),
    }
    if tool_card is not None:
        payload["tool_card"] = asdict(tool_card)
    if subagent_card is not None:
        payload["subagent_card"] = asdict(subagent_card)
    return payload


@dataclass(frozen=True, slots=True)
class SubagentTaskSnapshot:
    task_id: str
    name: str
    directive: str
    role: str
    status: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    transcript_path: Path
    result_excerpt: str = ""
    result_text: str = ""
    error: str = ""
    parent_message_count: int = 0
    tool_cards: int = 0
    assistant_turns: int = 0

    @property
    def elapsed_s(self) -> float:
        start = self.started_at or self.created_at
        end = self.finished_at or time.monotonic()
        return max(0.0, end - start)


@dataclass(frozen=True, slots=True)
class SubagentTaskCounts:
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0

    @property
    def active(self) -> int:
        return self.queued + self.running

    @property
    def total(self) -> int:
        return self.active + self.completed + self.failed + self.cancelled


@dataclass(frozen=True, slots=True)
class SubagentNotification:
    task: SubagentTaskSnapshot


@dataclass(slots=True)
class _TaskState:
    task_id: str
    name: str
    directive: str
    role: str
    profile: Profile
    config: SubagentConfig
    context_snapshot: list[dict[str, Any]]
    transcript_path: Path
    created_at: float
    parent_message_count: int
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    result_excerpt: str = ""
    result_text: str = ""
    error: str = ""
    tool_cards: int = 0
    assistant_turns: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    child_chat: object | None = None
    notified: bool = False

    def snapshot(self) -> SubagentTaskSnapshot:
        return SubagentTaskSnapshot(
            task_id=self.task_id,
            name=self.name,
            directive=self.directive,
            role=self.role,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            transcript_path=self.transcript_path,
            result_excerpt=self.result_excerpt,
            result_text=self.result_text,
            error=self.error,
            parent_message_count=self.parent_message_count,
            tool_cards=self.tool_cards,
            assistant_turns=self.assistant_turns,
        )


class SubagentManager:
    """Owns background child-chat tasks for one foreground chat."""

    def __init__(
        self,
        *,
        max_model_tasks: int = 1,
        transcript_dir: Path | None = None,
        client_factory: Callable[[Profile], object] | None = None,
        settle_sleep_s: float = 0.05,
    ) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._tasks: dict[str, _TaskState] = {}
        self._notifications: list[SubagentNotification] = []
        self._max_model_tasks = max(1, int(max_model_tasks))
        self._semaphore = threading.Semaphore(self._max_model_tasks)
        self._pending_max_model_tasks: int | None = None
        self._transcript_dir = transcript_dir or _default_transcript_dir()
        self._client_factory = client_factory or _default_client_factory
        self._settle_sleep_s = settle_sleep_s

    def reconfigure(self, *, max_model_tasks: int) -> bool:
        """Update queue width once the manager is idle.

        Returns True when the new width was applied, False when active
        work made the change unsafe to apply in-place.
        """
        requested = max(1, int(max_model_tasks))
        with self._lock:
            if any(
                task.thread is not None and task.thread.is_alive()
                for task in self._tasks.values()
            ):
                self._pending_max_model_tasks = requested
                return False
            self._apply_queue_width_locked(requested)
            return True

    def spawn_fork(
        self,
        *,
        directive: str,
        name: str = "",
        role: str = "worker",
        context_snapshot: list[dict[str, Any]],
        profile: Profile,
        config: SubagentConfig,
    ) -> SubagentTaskSnapshot:
        directive = directive.strip()
        normalized_role = normalize_subagent_role(role)
        created_at = time.monotonic()
        task_id = self._new_task_id()
        transcript_path = self._transcript_dir / f"{_now_ts()}-{task_id}.json"
        state = _TaskState(
            task_id=task_id,
            name=" ".join(name.split()),
            directive=directive,
            role=normalized_role,
            profile=profile,
            config=config,
            context_snapshot=list(context_snapshot),
            transcript_path=transcript_path,
            created_at=created_at,
            parent_message_count=len(context_snapshot),
        )
        thread = threading.Thread(
            target=self._run_task,
            args=(state,),
            daemon=True,
            name=f"successor-subagent-{task_id}",
        )
        state.thread = thread
        with self._lock:
            self._tasks[task_id] = state
        thread.start()
        return state.snapshot()

    def snapshots(self) -> list[SubagentTaskSnapshot]:
        with self._lock:
            tasks = [task.snapshot() for task in self._tasks.values()]
        tasks.sort(key=lambda task: task.created_at)
        return tasks

    def get(self, task_id: str) -> SubagentTaskSnapshot | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.snapshot() if task is not None else None

    def counts(self) -> SubagentTaskCounts:
        counts = {
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        with self._lock:
            for task in self._tasks.values():
                counts[task.status] = counts.get(task.status, 0) + 1
        return SubagentTaskCounts(**counts)

    def has_active_tasks(self) -> bool:
        with self._lock:
            return any(
                task.thread is not None and task.thread.is_alive()
                for task in self._tasks.values()
            )

    def cancel(self, target: str) -> int:
        with self._lock:
            if target == "all":
                tasks = list(self._tasks.values())
            else:
                task = self._tasks.get(target)
                tasks = [task] if task is not None else []
        cancelled = 0
        for task in tasks:
            if task is None:
                continue
            if task.status in ("completed", "failed", "cancelled"):
                continue
            task.stop.set()
            chat = task.child_chat
            if chat is not None:
                self._cancel_child_chat(chat)
            cancelled += 1
        return cancelled

    def drain_notifications(self) -> list[SubagentNotification]:
        with self._lock:
            out = list(self._notifications)
            self._notifications.clear()
        return out

    def _new_task_id(self) -> str:
        with self._lock:
            task_id = f"t{self._next_id:03d}"
            self._next_id += 1
        return task_id

    def _build_child_profile(self, task: _TaskState) -> Profile:
        tools = tuple(
            tool for tool in (task.profile.tools or ())
            if tool != "subagent"
        )
        tool_config = dict(task.profile.tool_config or {})
        if task.role == "verification":
            tools = tuple(
                tool for tool in tools
                if tool not in {"write_file", "edit_file"}
            )
            bash_config = dict(tool_config.get("bash") or {})
            bash_config["allow_mutating"] = False
            bash_config["allow_dangerous"] = False
            tool_config["bash"] = bash_config
        return replace(
            task.profile,
            tools=tools,
            tool_config=tool_config,
        )

    def _run_task(self, task: _TaskState) -> None:
        acquired = False
        try:
            while not task.stop.is_set():
                if self._semaphore.acquire(timeout=0.1):
                    acquired = True
                    break
            if task.stop.is_set():
                self._finish_cancelled(task, "cancelled before start")
                return

            self._update_task(task.task_id, status="running", started_at=time.monotonic())

            from ..chat import SuccessorChat, _Message

            child_profile = self._build_child_profile(task)
            child = SuccessorChat(
                profile=child_profile,
                client=self._client_factory(child_profile),
            )
            child.messages = [
                _Message(
                    snap["role"],
                    str(snap.get("content") or ""),
                    synthetic=bool(snap.get("synthetic", False)),
                    tool_card=snap.get("tool_card"),
                    subagent_card=snap.get("subagent_card"),
                    is_summary=bool(snap.get("is_summary", False)),
                    api_role_override=(
                        str(snap.get("api_role_override"))
                        if snap.get("api_role_override") is not None
                        else None
                    ),
                    display_text=(
                        str(snap.get("display_text"))
                        if snap.get("display_text") is not None
                        else None
                    ),
                )
                for snap in task.context_snapshot
            ]
            child.input_buffer = build_child_prompt(task.directive, role=task.role)
            with self._lock:
                task.child_chat = child
            child._submit()

            deadline = time.monotonic() + task.config.timeout_s
            cancel_deadline: float | None = None

            while time.monotonic() < deadline:
                if task.stop.is_set():
                    self._cancel_child_chat(child)
                    if cancel_deadline is None:
                        cancel_deadline = time.monotonic() + 1.5
                child._pump_stream()
                child._pump_running_tools()
                child._poll_compaction_worker()
                if child._cache_warmer is not None and child._cache_warmer.is_done():
                    child._cache_warmer = None
                settled = (
                    child._stream is None
                    and child._agent_turn == 0
                    and not child._running_tools
                    and child._compaction_worker is None
                )
                if cancel_deadline is not None and settled:
                    self._finish_cancelled(task, "cancelled")
                    self._write_transcript(task, child)
                    return
                if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                    self._finish_cancelled(task, "cancelled")
                    self._write_transcript(task, child)
                    return
                if settled:
                    break
                time.sleep(self._settle_sleep_s)
            else:
                self._cancel_child_chat(child)
                self._finish_failed(
                    task,
                    f"timed out after {task.config.timeout_s:.1f}s",
                    child,
                )
                self._write_transcript(task, child)
                return

            if child._cache_warmer is not None:
                child._cache_warmer.close()
                child._cache_warmer = None

            self._finish_completed(task, child)
            self._write_transcript(task, child)
        except Exception as exc:  # noqa: BLE001
            self._update_task(
                task.task_id,
                status="failed",
                finished_at=time.monotonic(),
                error=f"{type(exc).__name__}: {exc}",
            )
            self._queue_notification(task.task_id)
        finally:
            current_thread = threading.current_thread()
            with self._lock:
                task.child_chat = None
            if acquired:
                self._semaphore.release()
            self._apply_pending_queue_width_if_idle(
                exclude_thread=current_thread,
            )

    def _apply_queue_width_locked(self, max_model_tasks: int) -> None:
        self._max_model_tasks = max(1, int(max_model_tasks))
        self._semaphore = threading.Semaphore(self._max_model_tasks)
        self._pending_max_model_tasks = None

    def _apply_pending_queue_width_if_idle(
        self,
        *,
        exclude_thread: threading.Thread | None = None,
    ) -> None:
        with self._lock:
            pending = self._pending_max_model_tasks
            if pending is None:
                return
            if any(
                task.thread is not None
                and task.thread.is_alive()
                and task.thread is not exclude_thread
                for task in self._tasks.values()
            ):
                return
            self._apply_queue_width_locked(pending)

    def _finish_completed(self, task: _TaskState, child: object) -> None:
        messages = getattr(child, "messages", [])
        tool_cards = sum(
            1 for msg in messages
            if getattr(msg, "tool_card", None) is not None
        )
        assistant_turns = sum(
            1 for msg in messages
            if getattr(msg, "role", "") == "successor"
            and not getattr(msg, "synthetic", False)
            and not getattr(msg, "is_summary", False)
            and getattr(msg, "tool_card", None) is None
        )
        final_text = ""
        for msg in reversed(messages):
            if (
                getattr(msg, "role", "") == "successor"
                and not getattr(msg, "synthetic", False)
                and getattr(msg, "tool_card", None) is None
            ):
                final_text = str(getattr(msg, "raw_text", "") or "")
                if final_text.strip():
                    break
        if not final_text:
            final_text = "completed without a final assistant message"
        self._update_task(
            task.task_id,
            status="completed",
            finished_at=time.monotonic(),
            result_excerpt=_excerpt(final_text),
            result_text=final_text,
            tool_cards=tool_cards,
            assistant_turns=assistant_turns,
        )
        self._queue_notification(task.task_id)

    def _finish_failed(self, task: _TaskState, error: str, child: object | None = None) -> None:
        excerpt = ""
        if child is not None:
            messages = getattr(child, "messages", [])
            for msg in reversed(messages):
                if getattr(msg, "role", "") == "successor":
                    excerpt = _excerpt(str(getattr(msg, "raw_text", "") or ""))
                    if excerpt:
                        break
        self._update_task(
            task.task_id,
            status="failed",
            finished_at=time.monotonic(),
            error=error,
            result_excerpt=excerpt,
            result_text=excerpt,
        )
        self._queue_notification(task.task_id)

    def _finish_cancelled(self, task: _TaskState, reason: str) -> None:
        self._update_task(
            task.task_id,
            status="cancelled",
            finished_at=time.monotonic(),
            error=reason,
        )
        self._queue_notification(task.task_id)

    def _queue_notification(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.notified or not task.config.notify_on_finish:
                return
            task.notified = True
            self._notifications.append(SubagentNotification(task.snapshot()))

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks[task_id]
            for key, value in changes.items():
                setattr(task, key, value)

    def _cancel_child_chat(self, chat: object) -> None:
        stream = getattr(chat, "_stream", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        try:
            chat._cancel_running_tools()
        except Exception:
            pass
        compaction_worker = getattr(chat, "_compaction_worker", None)
        if compaction_worker is not None:
            try:
                compaction_worker.close()
            except Exception:
                pass
            chat._compaction_worker = None
            chat._compaction_anim = None
        cache_warmer = getattr(chat, "_cache_warmer", None)
        if cache_warmer is not None:
            try:
                cache_warmer.close()
            except Exception:
                pass
            chat._cache_warmer = None
        chat._pending_continuation = False
        chat._pending_agent_turn_after_compact = False
        chat._agent_turn = 0

    def _write_transcript(self, task: _TaskState, child: object) -> None:
        try:
            task.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "task_id": task.task_id,
                "directive": task.directive,
                "name": task.name,
                "status": task.status,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "profile": task.profile.name,
                "provider": dict(task.profile.provider) if task.profile.provider else None,
                "subagents": task.config.to_dict(),
                "result_excerpt": task.result_excerpt,
                "result_text": task.result_text,
                "error": task.error,
                "tool_cards": task.tool_cards,
                "assistant_turns": task.assistant_turns,
                "messages": [
                    _serialize_message(msg)
                    for msg in getattr(child, "messages", [])
                ],
            }
            task.transcript_path.write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
