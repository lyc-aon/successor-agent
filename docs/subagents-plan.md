# Subagents Design Notes

Current state on 2026-04-08: Successor now ships a real serial
background-subagent runtime. This document records what is actually
implemented, what deterministic references shaped it, and what should
come next.

## Deterministic sources

### Local Successor runtime

- `src/successor/chat.py` remains the main chat/runtime surface.
- `src/successor/subagents/manager.py` owns background child tasks.
- `src/successor/tools_registry.py` is the source of truth for the
  model-visible `subagent` tool.
- `src/successor/wizard/config.py` and the setup wizard tools step are
  the user-facing configuration surfaces.

### Local llama.cpp on this machine

- `successor doctor` reports the local server reachable at
  `http://localhost:8080`.
- `/props` reports slot support and `total_slots = 4`.
- `/slots` returns visible slot state.
- A tiny 2-request overlap probe completed, but did not reduce wall
  time relative to serial sends.
- A heavier 2-request generation probe was slower when run
  concurrently than when run serially:
  - serial: `17.682s`
  - concurrent: `21.692s`
- Conclusion: slot support is real, but "more concurrent generations"
  is not automatically "faster" on this local box. The foundation
  should stay serial-by-default and treat slot-aware parallelism as an
  adaptive optimization, not a semantic requirement.

### free-code patterns copied intentionally

- Forked children inherit parent context rather than receiving a blank
  prompt plus a giant re-brief.
- Completion arrives later as a notification event, not as a thing the
  parent fabricates immediately after spawn.
- The parent is told not to peek at a running child transcript.
- Child runs are isolated and transcripted.

### free-code patterns adapted, not copied 1:1

- Anthropic prompt-cache tricks do not map directly to local llama.cpp.
  The local optimization path is slot-aware scheduling, not byte-exact
  prompt-prefix games.
- Successor keeps the semantic model from free-code:
  inherited context, background notifications, isolated transcripts,
  and no recursive delegation in the child.

## What ships now

### Manual path

- `/fork <directive>` spawns a background child chat that inherits the
  current chat context.
- `/tasks` shows queued, running, completed, failed, and cancelled
  tasks with transcript paths.
- `/task-cancel <id|all>` cancels queued or running tasks.

### Model-visible path

- `subagent` is a real native tool in `tools_registry.py`.
- The tool is exposed to the model only when:
  - `profile.subagents.enabled` is on
  - `profile.subagents.notify_on_finish` is on
  - the profile tool list includes `subagent`
- The plain `default` profile leaves model delegation off by default.
- `successor-dev` enables the model-visible `subagent` tool by default.

### Runtime contract

- A child runs inside a headless `SuccessorChat`, not a toy executor.
- The child inherits the parent conversation snapshot.
- The child tool list strips `subagent` to prevent recursive forking.
- Spawn produces an inline subagent card plus a structured
  `<subagent-spawned>` tool result.
- Completion or failure comes back later as a structured
  `<subagent-notification>` event injected into the parent context as a
  user-role API message.
- Manual `/fork` still works when notifications are off, but the
  model-visible tool is hidden in that configuration because the parent
  would otherwise never receive the result.

### Scheduling

- The foundation scheduler is serial by default: `max_model_tasks`
  governs the number of concurrent child chats.
- Queue-width edits made while tasks are active are deferred until the
  manager goes idle, then applied safely.
- The foreground chat is not yet part of the same lease pool. That is a
  later optimization phase, not a correctness dependency.
- Given the current local measurements, the future slot-aware scheduler
  should default to one active model lane unless the user opts into
  broader fan-out or the runtime proves the extra slots help more than
  they hurt.

### Rendering

- The title bar shows a live task badge.
- Spawned subagents render as dedicated inline cards rather than fake
  bash cards.
- Completion notices render in the parent transcript as successor
  notices while serializing back to the model as user-role events.

## Verification completed

- Hermetic manager/chat/config/snapshot coverage exercises manual spawn,
  model-visible spawn, task listing, cancellation, transcript writing,
  notification injection, tool serialization, and deferred queue-width
  reconfiguration.
- Live local llama.cpp + Qwopus E2E passed for:
  - manual `/fork` summary flow
  - model uses `subagent`, then answers from the later notification
- Visual/plain artifacts from the live runs were inspected to confirm:
  - the task badge renders
  - the inline subagent card renders
  - the completion notification renders in the parent transcript

## Next phases

1. Adaptive slot-aware scheduling for llama.cpp, with sticky parent
   lanes and serial fallback.
2. Better task inspection UI: open transcript, compact old tasks,
   richer completion summaries.
3. Parallel read-only execution inside a subagent turn, so one worker
   can inspect multiple files or directories concurrently without
   paying for multiple model streams.
4. Optional scoped write isolation via worktrees for mutating child
   tasks.
5. Purpose-built higher-level subagent types, if the generic fork path
   proves insufficient.
