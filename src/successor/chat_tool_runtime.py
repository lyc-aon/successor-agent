"""Native tool/runtime orchestration extracted from SuccessorChat.

This module keeps the current runtime behavior intact while pulling the
tool execution subsystem out of `chat.py`. The chat remains the owner of
state and trace sinks; this helper owns spawn/dispatch/finalize/cancel
behavior for native tools and bash runners.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from .bash import (
    BashConfig,
    DangerousCommandRefused,
    MutatingCommandRefused,
    RefusedCommand,
    ToolCard,
    resolve_bash_config,
)
from .bash.change_capture import begin_change_capture, finalize_change_capture
from .bash.exec import _new_tool_call_id
from .bash.parser import parse_bash
from .bash.risk import classify_risk, max_risk
from .bash.runner import BashRunner, RunnerErrored, RunnerStarted
from .file_tools import (
    build_file_tool_recovery_nudge,
    edit_file_preview_card,
    normalize_file_path,
    note_non_read_tool_call,
    read_file_preview_card,
    run_edit_file,
    run_read_file,
    run_write_file,
    write_file_preview_card,
)
from .progress import ProgressUpdate, combine_progress_updates, summarize_tool_completion
from .runbook import (
    RunbookError,
    build_runbook_artifact,
    build_runbook_card_output,
    build_runbook_tool_result,
    experiment_attempt_to_payload,
    parse_experiment_attempt,
    parse_runbook_state,
    runbook_state_to_payload,
)
from .session_trace import clip_text as _trace_clip_text
from .skills import (
    build_skill_card_output,
    build_skill_reuse_result,
    build_skill_tool_result,
)
from .subagents.cards import SubagentToolCard
from .subagents.prompt import build_spawn_result_display, build_spawn_result_payload
from .tasks import (
    TaskLedgerError,
    build_task_card_output,
    build_task_tool_result,
    parse_task_items,
    task_items_to_payload,
)
from .tool_runner import CallableToolRunner
from .verification_contract import (
    VerificationContractError,
    build_assertions_artifact,
    build_verification_card_output,
    build_verification_tool_result,
    parse_verification_items,
    verification_items_to_payload,
)
from .web import (
    browser_preview_card,
    holonet_preview_card,
    resolve_browser_config,
    resolve_holonet_config,
    resolve_vision_config,
    resolve_route as resolve_holonet_route,
    vision_preview_card,
)
from .web.verification import build_verification_nudge


class ChatToolRuntime:
    """Owns tool execution flow while the chat owns state."""

    def __init__(self, host: Any, message_cls: type[Any]) -> None:
        self._host = host
        self._message_cls = message_cls

    def _message(self, role: str, raw_text: str, **kwargs: Any) -> Any:
        return self._message_cls(role, raw_text, **kwargs)

    def _append(self, message: Any) -> None:
        self._host.messages.append(message)

    def _append_successor(self, text: str, *, synthetic: bool = True) -> None:
        self._append(self._message("successor", text, synthetic=synthetic))

    def _append_tool_card(self, card: ToolCard, **kwargs: Any) -> None:
        self._append(self._message("tool", "", tool_card=card, **kwargs))

    def _append_running_tool(self, preview: ToolCard, runner: Any) -> None:
        msg = self._message("tool", "", tool_card=preview, running_tool=runner)
        self._append(msg)
        self._host._running_tools.append(msg)

    def handle_browser_verification_result(
        self,
        card: ToolCard,
        metadata: dict[str, Any],
    ) -> None:
        if not self._host._browser_verification_active:
            return
        intervention = metadata.get("verification_intervention")
        if not isinstance(intervention, dict):
            return
        self._host._trace_event(
            "browser_verification_intervention",
            turn=self._host._agent_turn,
            reason=self._host._browser_verification_reason,
            kind=str(intervention.get("kind") or ""),
            action=str(card.tool_arguments.get("action") or ""),
            target=str(card.tool_arguments.get("target") or card.tool_arguments.get("url") or ""),
            recommended_action=str(intervention.get("recommended_action") or ""),
        )
        if self._host._verification_continue_nudged_this_turn:
            return
        nudge = build_verification_nudge(intervention)
        if not nudge:
            return
        self._host._verification_continue_nudged_this_turn = True
        self._host._verification_continue_nudge = nudge

    def emit_completed_tool_batch_progress(
        self,
        completed: list[tuple[str, ToolCard, dict[str, Any]]],
    ) -> None:
        updates: list[ProgressUpdate] = []
        for tool_name, card, metadata in completed:
            if tool_name == "browser":
                self.handle_browser_verification_result(card, metadata)
            update = summarize_tool_completion(card, metadata=metadata)
            if update is not None:
                updates.append(update)
        summary = combine_progress_updates(updates)
        if summary is None:
            self._host._trace_event(
                "progress_summary_skipped",
                turn=self._host._agent_turn,
                source="tool_batch",
                reason="not_meaningful",
                tool_names=[tool_name for tool_name, _, _ in completed],
                update_count=len(updates),
            )
            return
        self._host._emit_progress_summary(
            summary,
            source="tool_batch",
            detail={
                "tool_names": [tool_name for tool_name, _, _ in completed],
                "update_count": len(updates),
            },
        )

    def spawn_bash_runner(
        self,
        command: str,
        *,
        bash_cfg: BashConfig,
        tool_call_id: str | None = None,
    ) -> bool:
        try:
            parsed = parse_bash(command)
        except Exception as exc:
            self._append_successor(f"bash parse failed for {command!r}: {exc}")
            return False

        classifier_risk, classifier_reason = classify_risk(command)
        final_risk = max_risk(parsed.risk, classifier_risk)
        resolved_call_id = tool_call_id or _new_tool_call_id()
        preview = replace(parsed, risk=final_risk, tool_call_id=resolved_call_id)

        if final_risk == "dangerous" and not bash_cfg.allow_dangerous:
            refused = DangerousCommandRefused(
                preview,
                classifier_reason or "command pattern flagged as dangerous",
            )
            self._host._trace_event(
                "bash_refused",
                tool_call_id=resolved_call_id,
                risk=final_risk,
                reason=refused.reason,
                command=command,
            )
            self._append_tool_card(refused.card)
            hint = self.refusal_hint(refused, bash_cfg)
            self._append_successor(f"refused: {refused.reason}. {hint}")
            return False
        if final_risk == "mutating" and not bash_cfg.allow_mutating:
            refused = MutatingCommandRefused(
                preview,
                classifier_reason or "mutating command refused in read-only mode",
            )
            self._host._trace_event(
                "bash_refused",
                tool_call_id=resolved_call_id,
                risk=final_risk,
                reason=refused.reason,
                command=command,
            )
            self._append_tool_card(refused.card)
            hint = self.refusal_hint(refused, bash_cfg)
            self._append_successor(f"refused: {refused.reason}. {hint}")
            return False

        runner = BashRunner(
            command,
            cwd=bash_cfg.working_directory,
            timeout=bash_cfg.timeout_s,
            max_output_bytes=bash_cfg.max_output_bytes,
            tool_call_id=resolved_call_id,
        )
        runner.change_capture = begin_change_capture(
            preview,
            cwd=bash_cfg.working_directory,
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "bash_spawn",
            tool_call_id=resolved_call_id,
            verb=preview.verb,
            risk=preview.risk,
            parser=preview.parser_name,
            cwd=bash_cfg.working_directory or os.getcwd(),
            timeout_s=bash_cfg.timeout_s,
            command=command,
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def dispatch_streamed_bash_blocks(self, blocks: list[str]) -> bool:
        if not blocks:
            return False
        bash_cfg = resolve_bash_config(self._host.profile)
        any_ran = False
        for command in blocks:
            if self.spawn_bash_runner(command, bash_cfg=bash_cfg):
                any_ran = True
        return any_ran

    def spawn_subagent_task(
        self,
        prompt: str,
        *,
        name: str = "",
        tool_call_id: str | None = None,
    ) -> bool:
        cfg = self._host.profile.subagents
        if not cfg.enabled:
            self._append_successor(
                "subagent tool is disabled for this profile. Enable subagents in /config before delegating background work.",
            )
            return False
        if not cfg.notify_on_finish:
            self._append_successor(
                "subagent tool requires notify_on_finish=on so the parent chat can receive the result later.",
            )
            return False
        directive = prompt.strip()
        if not directive:
            self._append_successor("subagent tool call had no prompt.")
            return False

        task = self._host._subagent_manager.spawn_fork(
            directive=directive,
            name=name,
            context_snapshot=self._host._subagent_context_snapshot(),
            profile=self._host.profile,
            config=cfg,
        )
        card = SubagentToolCard(
            task_id=task.task_id,
            name=task.name,
            directive=directive,
            tool_call_id=tool_call_id or _new_tool_call_id(),
            spawn_result=build_spawn_result_payload(task),
        )
        self._append(
            self._message(
                "tool",
                "",
                subagent_card=card,
                display_text=build_spawn_result_display(task),
            )
        )
        self._host._scroll_to_bottom()
        return True

    def tool_error_card(
        self,
        *,
        tool_name: str,
        verb: str,
        raw_command: str,
        tool_call_id: str,
        params: tuple[tuple[str, str], ...],
        tool_arguments: dict[str, Any],
        raw_label_prefix: str,
        message: str,
        risk: str = "safe",
    ) -> ToolCard:
        return ToolCard(
            verb=verb,
            params=params,
            risk=risk,
            raw_command=raw_command,
            confidence=1.0,
            parser_name=f"native-{tool_name}",
            stderr=message,
            exit_code=1,
            duration_ms=0.0,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            raw_label_prefix=raw_label_prefix,
            tool_call_id=tool_call_id,
        )

    def spawn_skill_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        requested_name = str(arguments.get("skill") or "").strip().lower()
        task = " ".join(str(arguments.get("task") or "").split()).strip()
        enabled_skills = {
            skill.name: skill
            for skill in self._host._enabled_skills_for_turn()
        }
        skill = enabled_skills.get(requested_name)
        if skill is None:
            card = self.tool_error_card(
                tool_name="skill",
                verb="load-skill",
                raw_command=requested_name or "skill",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments={
                    "skill": requested_name,
                    **({"task": task} if task else {}),
                },
                raw_label_prefix="§",
                message=(
                    f"skill '{requested_name or '(missing)'}' is not enabled for "
                    "this profile, is missing on disk, or requires tools that are "
                    "not available in this turn."
                ),
            )
            self._append_tool_card(card)
            return False

        params: list[tuple[str, str]] = [("skill", skill.name)]
        if skill.allowed_tools:
            params.append(("tools", ", ".join(skill.allowed_tools)))
        if task:
            task_value = task if len(task) <= 64 else task[:63].rstrip() + "…"
            params.append(("task", task_value))
        raw_command = " ".join(bit for bit in (skill.name, task) if bit)
        preview = ToolCard(
            verb="load-skill",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-skill",
            tool_name="skill",
            tool_arguments={
                "skill": skill.name,
                **({"task": task} if task else {}),
            },
            raw_label_prefix="§",
            tool_call_id=resolved_call_id,
        )

        source = "builtin"
        source_path = getattr(skill, "source_path", "")
        if "/.config/" in source_path:
            source = "user"

        if self._host._skill_already_loaded(skill.name):
            final_card = replace(
                preview,
                output=f"Skill `{skill.name}` is already loaded earlier in the conversation.",
                exit_code=0,
                duration_ms=0.0,
                api_content_override=build_skill_reuse_result(skill.name, task=task),
            )
            self._append_tool_card(final_card)
            self._host._scroll_to_bottom()
            return True

        final_card = replace(
            preview,
            output=build_skill_card_output(skill, task=task, source=source),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_skill_tool_result(
                skill,
                task=task,
                source=source,
            ),
        )
        self._host._trace_event(
            "tool_spawn",
            tool_name="skill",
            tool_call_id=resolved_call_id,
            skill_name=skill.name,
            task=task,
        )
        self._host._trace_event(
            "tool_runner_finished",
            tool_name="skill",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self._append_tool_card(final_card)
        self._host._scroll_to_bottom()
        return True

    def spawn_task_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        try:
            items = parse_task_items(arguments.get("items"))
        except TaskLedgerError as exc:
            card = self.tool_error_card(
                tool_name="task",
                verb="task-ledger",
                raw_command="update tasks",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments={"items": arguments.get("items")},
                raw_label_prefix="#",
                message=str(exc),
            )
            self._append_tool_card(card)
            return False

        self._host._task_ledger.replace(items)
        active = self._host._task_ledger.in_progress_task()
        params: list[tuple[str, str]] = [("tasks", str(len(items)))]
        if active is not None:
            active_value = active.content
            if len(active_value) > 64:
                active_value = active_value[:63].rstrip() + "…"
            params.append(("active", active_value))
        raw_command = "clear" if not items else f"update {len(items)} tasks"
        payload = {"items": task_items_to_payload(items)}
        preview = ToolCard(
            verb="task-ledger",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-task",
            tool_name="task",
            tool_arguments=payload,
            raw_label_prefix="#",
            tool_call_id=resolved_call_id,
        )
        final_card = replace(
            preview,
            output=build_task_card_output(self._host._task_ledger),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_task_tool_result(self._host._task_ledger),
        )
        self._host._trace_event(
            "tool_spawn",
            tool_name="task",
            tool_call_id=resolved_call_id,
            task_count=len(items),
            active_task=active.active_form if active else "",
        )
        self._host._trace_event(
            "task_ledger_updated",
            tool_call_id=resolved_call_id,
            task_count=len(items),
            open_count=self._host._task_ledger.open_count(),
            completed_count=self._host._task_ledger.completed_count(),
            active_task=active.active_form if active else "",
        )
        self._host._trace_event(
            "tool_runner_finished",
            tool_name="task",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self._append_tool_card(final_card)
        self._host._scroll_to_bottom()
        return True

    def spawn_verify_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        try:
            items = parse_verification_items(arguments.get("items"))
        except VerificationContractError as exc:
            card = self.tool_error_card(
                tool_name="verify",
                verb="verification",
                raw_command="update verification contract",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments={"items": arguments.get("items")},
                raw_label_prefix="✓",
                message=str(exc),
            )
            self._append_tool_card(card)
            return False

        self._host._verification_ledger.replace(items)
        active = self._host._verification_ledger.in_progress_item()
        params: list[tuple[str, str]] = [("assertions", str(len(items)))]
        if active is not None:
            active_value = active.claim
            if len(active_value) > 64:
                active_value = active_value[:63].rstrip() + "…"
            params.append(("active", active_value))
        raw_command = "clear" if not items else f"update {len(items)} assertions"
        payload = {"items": verification_items_to_payload(items)}
        preview = ToolCard(
            verb="verification",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-verify",
            tool_name="verify",
            tool_arguments=payload,
            raw_label_prefix="✓",
            tool_call_id=resolved_call_id,
        )
        final_card = replace(
            preview,
            output=build_verification_card_output(self._host._verification_ledger),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_verification_tool_result(self._host._verification_ledger),
        )
        assertions_artifact = build_assertions_artifact(self._host._verification_ledger)
        self._host._trace_event(
            "tool_spawn",
            tool_name="verify",
            tool_call_id=resolved_call_id,
            assertion_count=len(items),
            active_claim=active.claim if active else "",
            active_evidence=active.evidence if active else "",
        )
        self._host._trace_event(
            "verification_contract_updated",
            tool_call_id=resolved_call_id,
            assertion_count=len(items),
            pending_count=self._host._verification_ledger.pending_count(),
            open_count=self._host._verification_ledger.open_count(),
            passed_count=self._host._verification_ledger.passed_count(),
            failed_count=self._host._verification_ledger.failed_count(),
            active_claim=active.claim if active else "",
            active_evidence=active.evidence if active else "",
            items=payload["items"],
            artifact=assertions_artifact,
        )
        self._host._trace_event(
            "tool_runner_finished",
            tool_name="verify",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self._append_tool_card(final_card)
        self._host._scroll_to_bottom()
        return True

    def spawn_runbook_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        try:
            state = parse_runbook_state(arguments)
            if state is None and arguments.get("attempt") not in (None, ""):
                raise RunbookError(
                    "runbook.attempt cannot be recorded in the same call that clears the runbook"
                )
            attempt = parse_experiment_attempt(
                arguments.get("attempt"),
                next_attempt_id=self._host._runbook_attempt_count + 1,
            )
        except RunbookError as exc:
            card = self.tool_error_card(
                tool_name="runbook",
                verb="runbook",
                raw_command="update runbook",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments=dict(arguments),
                raw_label_prefix="◇",
                message=str(exc),
            )
            self._append_tool_card(card)
            return False

        if state is None:
            self._host._runbook.clear()
            self._host._runbook_attempt_count = 0
        else:
            self._host._runbook.replace(state)
        if attempt is not None:
            self._host._runbook_attempt_count = attempt.attempt_id

        state_payload = runbook_state_to_payload(self._host._runbook.state)
        payload: dict[str, Any] = dict(state_payload)
        if attempt is not None:
            payload["attempt"] = experiment_attempt_to_payload(attempt)
        params: list[tuple[str, str]] = []
        if self._host._runbook.state is not None:
            params.append(("status", self._host._runbook.state.status))
            params.append(("baseline", self._host._runbook.state.baseline_status))
            if self._host._runbook.state.evaluator:
                params.append(("eval", str(len(self._host._runbook.state.evaluator))))
        else:
            params.append(("state", "cleared"))
        if attempt is not None:
            params.append(("attempt", str(attempt.attempt_id)))
            params.append(("decision", attempt.decision))
        raw_command = "clear" if self._host._runbook.state is None else "update runbook"
        preview = ToolCard(
            verb="runbook",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-runbook",
            tool_name="runbook",
            tool_arguments=payload,
            raw_label_prefix="◇",
            tool_call_id=resolved_call_id,
        )
        runbook_artifact = build_runbook_artifact(
            self._host._runbook.state,
            attempt_count=self._host._runbook_attempt_count,
            last_attempt=attempt,
        )
        final_card = replace(
            preview,
            output=build_runbook_card_output(self._host._runbook.state, attempt=attempt),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_runbook_tool_result(
                self._host._runbook.state,
                attempt=attempt,
            ),
        )
        objective = (
            self._host._runbook.state.objective
            if self._host._runbook.state is not None else ""
        )
        self._host._trace_event(
            "tool_spawn",
            tool_name="runbook",
            tool_call_id=resolved_call_id,
            objective=objective,
            baseline_status=(
                self._host._runbook.state.baseline_status
                if self._host._runbook.state is not None else ""
            ),
            attempt_id=attempt.attempt_id if attempt is not None else 0,
        )
        self._host._trace_event(
            "runbook_updated",
            tool_call_id=resolved_call_id,
            objective=objective,
            status=(
                self._host._runbook.state.status
                if self._host._runbook.state is not None else "cleared"
            ),
            baseline_status=(
                self._host._runbook.state.baseline_status
                if self._host._runbook.state is not None else "missing"
            ),
            active_hypothesis=(
                self._host._runbook.state.active_hypothesis
                if self._host._runbook.state is not None else ""
            ),
            evaluator_count=(
                len(self._host._runbook.state.evaluator)
                if self._host._runbook.state is not None else 0
            ),
            attempt_count=self._host._runbook_attempt_count,
            runbook=state_payload,
            artifact=runbook_artifact,
        )
        if attempt is not None:
            self._host._trace_event(
                "experiment_attempt_recorded",
                tool_call_id=resolved_call_id,
                objective=objective,
                attempt=experiment_attempt_to_payload(attempt),
                baseline_status=(
                    self._host._runbook.state.baseline_status
                    if self._host._runbook.state is not None else "missing"
                ),
            )
        self._host._trace_event(
            "tool_runner_finished",
            tool_name="runbook",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self._append_tool_card(final_card)
        self._host._scroll_to_bottom()
        return True

    def spawn_read_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        working_directory = self._host._tool_working_directory()
        requested_path = str(arguments.get("file_path") or "").strip()
        try:
            normalized_path = normalize_file_path(
                requested_path,
                working_directory=working_directory,
            )
        except Exception as exc:
            card = self.tool_error_card(
                tool_name="read_file",
                verb="read-file",
                raw_command=requested_path or "read_file",
                tool_call_id=resolved_call_id,
                params=(("path", requested_path),) if requested_path else (),
                tool_arguments=dict(arguments),
                raw_label_prefix="⟫",
                message=str(exc),
            )
            self._append_tool_card(card)
            return False

        normalized_args = dict(arguments)
        normalized_args["file_path"] = normalized_path
        preview = read_file_preview_card(normalized_args, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_read_file(
                normalized_args,
                preview=preview,
                read_state=self._host._file_read_state,
                read_tracker=self._host._file_read_tracker,
                working_directory=working_directory,
                progress=progress,
            ),
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "tool_spawn",
            tool_name="read_file",
            tool_call_id=resolved_call_id,
            path=normalized_path,
            offset=normalized_args.get("offset"),
            limit=normalized_args.get("limit"),
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def spawn_write_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        working_directory = self._host._tool_working_directory()
        requested_path = str(arguments.get("file_path") or "").strip()
        try:
            normalized_path = normalize_file_path(
                requested_path,
                working_directory=working_directory,
            )
        except Exception as exc:
            card = self.tool_error_card(
                tool_name="write_file",
                verb="write-file",
                raw_command=requested_path or "write_file",
                tool_call_id=resolved_call_id,
                params=(("path", requested_path),) if requested_path else (),
                tool_arguments=dict(arguments),
                raw_label_prefix="✎",
                message=str(exc),
                risk="mutating",
            )
            self._append_tool_card(card)
            return False

        normalized_args = dict(arguments)
        normalized_args["file_path"] = normalized_path
        preview = write_file_preview_card(normalized_args, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_write_file(
                normalized_args,
                preview=preview,
                read_state=self._host._file_read_state,
                working_directory=working_directory,
                progress=progress,
            ),
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "tool_spawn",
            tool_name="write_file",
            tool_call_id=resolved_call_id,
            path=normalized_path,
            content_length=len(str(normalized_args.get("content") or "")),
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def spawn_edit_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        working_directory = self._host._tool_working_directory()
        requested_path = str(arguments.get("file_path") or "").strip()
        try:
            normalized_path = normalize_file_path(
                requested_path,
                working_directory=working_directory,
            )
        except Exception as exc:
            card = self.tool_error_card(
                tool_name="edit_file",
                verb="edit-file",
                raw_command=requested_path or "edit_file",
                tool_call_id=resolved_call_id,
                params=(("path", requested_path),) if requested_path else (),
                tool_arguments=dict(arguments),
                raw_label_prefix="✎",
                message=str(exc),
                risk="mutating",
            )
            self._append_tool_card(card)
            return False

        normalized_args = dict(arguments)
        normalized_args["file_path"] = normalized_path
        preview = edit_file_preview_card(normalized_args, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_edit_file(
                normalized_args,
                preview=preview,
                read_state=self._host._file_read_state,
                working_directory=working_directory,
                progress=progress,
            ),
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "tool_spawn",
            tool_name="edit_file",
            tool_call_id=resolved_call_id,
            path=normalized_path,
            replace_all=bool(normalized_args.get("replace_all")),
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def spawn_holonet_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        cfg = resolve_holonet_config(self._host.profile)
        try:
            route = resolve_holonet_route(arguments, cfg)
        except Exception as exc:
            card = self.tool_error_card(
                tool_name="holonet",
                verb="web-search",
                raw_command="holonet",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments=dict(arguments),
                raw_label_prefix="≈",
                message=str(exc),
            )
            self._append_tool_card(card)
            return False

        preview = holonet_preview_card(route, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: self._host._run_holonet(route, cfg, progress),
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "tool_spawn",
            tool_name="holonet",
            tool_call_id=resolved_call_id,
            provider=route.provider,
            query=route.query,
            url=route.url,
            count=route.count,
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def spawn_browser_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        preview = browser_preview_card(arguments, tool_call_id=resolved_call_id)
        browser_cfg = resolve_browser_config(self._host.profile)
        browser_status = self._host._browser_runtime_status(browser_cfg)
        if not browser_status.package_available:
            card = self.tool_error_card(
                tool_name="browser",
                verb=preview.verb,
                raw_command=preview.raw_command,
                tool_call_id=resolved_call_id,
                params=preview.params,
                tool_arguments=preview.tool_arguments,
                raw_label_prefix=preview.raw_label_prefix,
                message=(
                    "Playwright is not available in the configured runtime. "
                    "Install with `pip install 'successor[browser]'`, or set "
                    "browser.python_executable to a Python interpreter that "
                    "already has Playwright installed."
                ),
            )
            self._append_tool_card(card)
            return False

        manager = self._host._browser_manager_for_profile()
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: self._host._run_browser_action(
                arguments,
                manager=manager,
                progress=progress,
            ),
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "tool_spawn",
            tool_name="browser",
            tool_call_id=resolved_call_id,
            action=str(arguments.get("action") or ""),
            target=str(arguments.get("target") or arguments.get("url") or ""),
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def spawn_vision_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        resolved_call_id = tool_call_id or _new_tool_call_id()
        preview = vision_preview_card(arguments, tool_call_id=resolved_call_id)
        vision_cfg = resolve_vision_config(self._host.profile)
        status = self._host._vision_runtime_status(vision_cfg)
        if not status.tool_available:
            card = self.tool_error_card(
                tool_name="vision",
                verb=preview.verb,
                raw_command=preview.raw_command,
                tool_call_id=resolved_call_id,
                params=preview.params,
                tool_arguments=preview.tool_arguments,
                raw_label_prefix=preview.raw_label_prefix,
                message=status.reason,
            )
            self._append_tool_card(card)
            return False

        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: self._host._run_vision_analysis(
                arguments,
                vision_cfg,
                progress=progress,
            ),
        )
        self._append_running_tool(preview, runner)
        self._host._trace_event(
            "tool_spawn",
            tool_name="vision",
            tool_call_id=resolved_call_id,
            path=str(arguments.get("path") or ""),
            prompt=str(arguments.get("prompt") or ""),
        )
        runner.start()
        self._host._scroll_to_bottom()
        return True

    def dispatch_native_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        stream_finish_reason: str = "stop",
        stream_finish_reason_reported: bool = True,
    ) -> bool:
        if not tool_calls:
            return False
        bash_cfg = resolve_bash_config(self._host.profile)
        any_ran = False
        for tc in tool_calls:
            name = tc.get("name") or ""
            args = tc.get("arguments") or {}
            call_id = tc.get("id") or ""

            if name != "bash":
                if name != "read_file":
                    note_non_read_tool_call(self._host._file_read_tracker)
                if name == "read_file":
                    if isinstance(args, dict) and self.spawn_read_file_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "write_file":
                    if isinstance(args, dict) and self.spawn_write_file_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "edit_file":
                    if isinstance(args, dict) and self.spawn_edit_file_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "task":
                    if isinstance(args, dict) and self.spawn_task_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "verify":
                    if isinstance(args, dict) and self.spawn_verify_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "runbook":
                    if isinstance(args, dict) and self.spawn_runbook_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "skill":
                    if isinstance(args, dict) and self.spawn_skill_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "subagent":
                    prompt = args.get("prompt") if isinstance(args, dict) else ""
                    label = args.get("name") if isinstance(args, dict) else ""
                    if self.spawn_subagent_task(
                        str(prompt or ""),
                        name=str(label or ""),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "holonet":
                    if isinstance(args, dict) and self.spawn_holonet_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "browser":
                    if isinstance(args, dict) and self.spawn_browser_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "vision":
                    if isinstance(args, dict) and self.spawn_vision_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                self._append_successor(
                    f"unknown tool {name!r} — supported tools are read_file, write_file, edit_file, bash, task, verify, runbook, skill, subagent, holonet, browser, and vision",
                )
                continue

            note_non_read_tool_call(self._host._file_read_tracker)
            command = args.get("command") if isinstance(args, dict) else ""
            if not command:
                self._append_successor(
                    self._host._native_tool_call_failure_message(
                        tc,
                        finish_reason=stream_finish_reason,
                        finish_reason_reported=stream_finish_reason_reported,
                    ),
                )
                continue

            if self.spawn_bash_runner(
                command,
                bash_cfg=bash_cfg,
                tool_call_id=call_id,
            ):
                any_ran = True
        return any_ran

    def pump_running_tools(self) -> None:
        if not self._host._running_tools:
            return

        completed_msgs: list[Any] = []
        completed_tool_batch: list[tuple[str, ToolCard, dict[str, Any]]] = []
        for msg in self._host._running_tools:
            runner = msg.running_tool
            if runner is None:
                completed_msgs.append(msg)
                continue
            for ev in runner.drain():
                if isinstance(ev, RunnerStarted):
                    tool_name = getattr(msg.tool_card, "tool_name", "bash")
                    if tool_name == "bash":
                        self._host._trace_event(
                            "bash_runner_started",
                            tool_call_id=runner.tool_call_id,
                            pid=runner.pid,
                        )
                    else:
                        self._host._trace_event(
                            "tool_runner_started",
                            tool_name=tool_name,
                            tool_call_id=runner.tool_call_id,
                        )
                elif isinstance(ev, RunnerErrored):
                    tool_name = getattr(msg.tool_card, "tool_name", "bash")
                    self._host._trace_event(
                        "tool_runner_errored" if tool_name != "bash" else "bash_runner_errored",
                        tool_name=tool_name,
                        tool_call_id=runner.tool_call_id,
                        message=ev.message,
                    )
            msg._card_rows_cache_key = None
            msg._card_rows_cache = None
            if runner.is_done():
                finalized = self.finalize_runner(msg)
                if finalized is not None:
                    completed_tool_batch.append(finalized)
                completed_msgs.append(msg)

        for msg in completed_msgs:
            try:
                self._host._running_tools.remove(msg)
            except ValueError:
                pass

        if completed_tool_batch:
            self.emit_completed_tool_batch_progress(completed_tool_batch)

        if (
            self._host._pending_continuation
            and not self._host._running_tools
            and self._host._agent_turn > 0
            and self._host._stream is None
        ):
            self._host._pending_continuation = False
            self._host._begin_agent_turn()

    def finalize_runner(
        self,
        msg: Any,
    ) -> tuple[str, ToolCard, dict[str, Any]] | None:
        runner = msg.running_tool
        preview = msg.tool_card
        if runner is None or preview is None:
            return None
        build_final = getattr(runner, "build_final_card", None)
        if callable(build_final):
            final_card = build_final(preview)
        else:
            stdout = runner.stdout
            stderr = runner.stderr
            exit_code = runner.exit_code if runner.exit_code is not None else -1
            if runner.error:
                if stderr and not stderr.endswith("\n"):
                    stderr = stderr + "\n"
                stderr = (stderr or "") + f"[{runner.error}]"
            final_card = replace(
                preview,
                output=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=runner.elapsed() * 1000.0,
                truncated=runner.truncated,
            )
            change_artifact = finalize_change_capture(
                getattr(runner, "change_capture", None),
            )
            if change_artifact is not None:
                final_card = replace(final_card, change_artifact=change_artifact)
        metadata = dict(getattr(runner, "metadata", None) or {})
        msg.tool_card = final_card
        msg.running_tool = None
        msg._card_rows_cache_key = None
        msg._card_rows_cache = None
        tool_name = getattr(final_card, "tool_name", "bash")
        event_name = "bash_runner_finished" if tool_name == "bash" else "tool_runner_finished"
        self._host._trace_event(
            event_name,
            tool_name=tool_name,
            tool_call_id=runner.tool_call_id,
            exit_code=final_card.exit_code,
            error=runner.error,
            duration_ms=round(runner.elapsed() * 1000.0, 3),
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt=_trace_clip_text(final_card.stderr, limit=320),
            truncated=final_card.truncated,
        )
        if tool_name in {"write_file", "edit_file"} and final_card.exit_code != 0:
            recovery_nudge = build_file_tool_recovery_nudge(
                tool_name,
                final_card.stderr or final_card.output or runner.error or "",
            )
            if recovery_nudge and not self._host._file_tool_continue_nudged_this_turn:
                self._host._file_tool_continue_nudged_this_turn = True
                self._host._file_tool_continue_nudge = recovery_nudge
                self._host._trace_event(
                    "file_tool_recovery_nudge",
                    turn=self._host._agent_turn,
                    tool_name=tool_name,
                    tool_call_id=runner.tool_call_id,
                    message=recovery_nudge,
                )
        return tool_name, final_card, metadata

    def cancel_running_tools(self) -> None:
        for msg in self._host._running_tools:
            if msg.running_tool is not None:
                msg.running_tool.cancel()

    def refusal_hint(
        self,
        exc: RefusedCommand,
        bash_cfg: BashConfig,
    ) -> str:
        if isinstance(exc, DangerousCommandRefused):
            if bash_cfg.allow_dangerous:
                return "enable bash.allow_dangerous in the profile to run."
            return (
                "enable bash.allow_dangerous in the profile to opt in "
                "(yolo mode) — /config → tools → bash."
            )
        if isinstance(exc, MutatingCommandRefused):
            return (
                "profile is in read-only mode. Enable bash.allow_mutating "
                "in /config → tools → bash to run this."
            )
        return ""
