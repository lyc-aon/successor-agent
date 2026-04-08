# Compaction

Successor's autocompactor keeps your conversation history below the
model's context window without you having to think about it. This
doc covers the user-facing knobs, the design behind them, and how
to extend or override the defaults.

## What it does

Two layers, both running automatically:

1. **Microcompact**. Every tick, the harness clears stale tool
   results from old rounds. Cheap, no model call, runs constantly.
2. **Autocompact**. When the conversation crosses a configured
   percentage of the context window, the harness pauses the next
   user turn, sends the older rounds to the model for summarization,
   and replaces them with one summary message + a verbatim copy of
   the most recent rounds. The user's pending message gets sent
   immediately after the new log lands.

The chat stays interactive during compaction. The fold animation
covers the work; you can press `Ctrl+G` to abort.

## The thresholds

Three thresholds, each defined as a percentage of the **resolved**
context window. The chat detects it from the active model
provider: `/props` on llama.cpp, `/v1/models` on OpenRouter, a
fallback table on OpenAI.

| Threshold | Default | What happens |
|-----------|---------|--------------|
| **warning** | window × 12.5% | The title-bar pill turns warm and shows the fill % |
| **autocompact** | window × 6.25% | A compaction worker fires before the next turn |
| **blocking** | window × 1.5625% | The chat refuses to send the request. You must compact manually or shorten the prompt. |

The percentages are buffer sizes. The threshold trips at
`used >= window - (window × pct)`. So 6.25% buffer means autocompact
at 93.75% full.

Each threshold also has a hard floor (`warning_floor=8000`,
`autocompact_floor=4000`, `blocking_floor=1000`) so a tiny
context window doesn't end up with a 0-token buffer.

## Configuring per profile

Edit `~/.config/successor/profiles/<name>.json` and add a
`compaction` block:

```json
{
  "name": "my-profile",
  "compaction": {
    "warning_pct": 0.20,
    "autocompact_pct": 0.10,
    "blocking_pct": 0.04,
    "warning_floor": 12000,
    "autocompact_floor": 6000,
    "blocking_floor": 2000,
    "enabled": true,
    "keep_recent_rounds": 6,
    "summary_max_tokens": 16000
  }
}
```

All fields are optional. Anything missing uses the default.
Anything malformed (wrong type, out-of-range value, threshold
ordering violation) silently falls back to defaults, and the rest
of the profile still loads. This is the same lenient-load policy
the profile parser uses everywhere.

### Field reference

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `warning_pct` | float | 0.125 | 12.5%, pill turns warm |
| `autocompact_pct` | float | 0.0625 | 6.25%, autocompact fires |
| `blocking_pct` | float | 0.015625 | 1.5625%, refuse the API call |
| `warning_floor` | int | 8000 | min warning buffer in tokens |
| `autocompact_floor` | int | 4000 | min autocompact buffer in tokens |
| `blocking_floor` | int | 1000 | min blocking buffer in tokens |
| `enabled` | bool | true | when false, NEVER autocompact proactively |
| `keep_recent_rounds` | int | 6 | how many recent rounds to preserve verbatim |
| `summary_max_tokens` | int | 16000 | max tokens the summary model may emit |

### Invariants

The dataclass enforces these at construction time. Violations
either fall back to defaults (when loaded from JSON) or raise
`ValueError` (when constructed directly in code):

- `warning_pct > autocompact_pct > blocking_pct >= 0`
- All percentages are in `[0, 1]`
- `warning_floor >= autocompact_floor >= blocking_floor >= 0`
- `keep_recent_rounds >= 1`
- `summary_max_tokens >= 256`

## Configuring at runtime

The chat's config menu (`Ctrl+,`) has a `compaction` section with
all six fields. Editing them updates the profile in memory and
marks the row as dirty; pressing `s` writes the profile back to
disk and reloads the registry. Percentage fields are entered as
percent (e.g. type `6.25` for 6.25%). The conversion to fraction
happens at commit time.

The setup wizard (`successor setup`) has a one-screen `compact`
step with four presets:

| Preset | warn / auto / block | Use when… |
|--------|--------------------|----|
| **default** | 12.5% / 6.25% / 1.5% | normal use |
| **aggressive** | 25% / 12.5% / 3% | slow models, or you want to never feel the slow-down |
| **lazy** | 5% / 2% / 0.5% | you'd rather lose context than pay the compaction cost early |
| **off** | n/a (disabled) | you'll trigger `/compact` manually |

Pick one in the wizard, fine-tune in the config menu later.

## How the percentage scaling works

The chat resolves the context window once at session start via
`SuccessorChat._resolve_context_window`. The result is cached on
the chat instance so the per-frame footer doesn't re-probe.

Whenever the chat needs a `ContextBudget` (e.g. to draw the fill
bar, to decide whether to autocompact), it calls
`SuccessorChat._agent_budget()`, which:

1. Reads the cached resolved window
2. Calls `self.profile.compaction.buffers_for_window(window)` to
   get the three buffer values, applying the floor system
3. Constructs a fresh `ContextBudget(window, *buffers)` and returns it

This is the only seam between the static profile config and the
runtime budget. Changing `self.profile` and re-calling
`_agent_budget()` produces a budget with the new thresholds. No
restart needed.

## How autocompact triggers

`SuccessorChat._begin_agent_turn()` opens with a call to
`_check_and_maybe_defer_for_autocompact()`. The gate decides:

```
defer if all of:
  profile.compaction.enabled
  no compaction worker is already in flight
  no autocompact has been attempted for this user message
  current token count >= autocompact threshold
  log has at least MIN_ROUNDS_TO_COMPACT rounds (4)
```

When deferred:

1. The per-turn guard `_autocompact_attempted_this_turn` is set
   so the gate can't fire twice for the same user message
2. The deferred-resume flag `_pending_agent_turn_after_compact`
   is set
3. A compaction worker is spawned (same path the manual `/compact`
   command uses, with `reason="auto"`)
4. The user's message stays in `self.messages` waiting

When the worker reports a result, `_poll_compaction_worker`:

1. Applies the new log via `_from_agent_log`
2. Updates the compaction animation to the materialize phase
3. If `_pending_agent_turn_after_compact` was set, re-enters
   `_begin_agent_turn` so the model gets the compacted prompt
4. Fires the cache pre-warmer for the post-compact prefix

If the worker fails, the gate still calls `_begin_agent_turn`
again. Reactive PTL recovery in the streaming layer may save the
turn, or the user gets a clear API error.

`Ctrl+G` during an in-flight autocompact aborts the worker AND
clears the deferred-resume flag, so the cancellation actually
cancels the pending turn instead of resuming it on a half-compacted
log.

## The post-compact size assertion

After `compact()` finishes, the function checks that the new log
is at least 10% smaller than the original. If it isn't, the
boundary marker gets a `warning` field set with a human-readable
message and the `underperformed` property returns True. The
boundary message in the new log gets a `⚠ underperformed` annotation
so the user can see what happened.

This catches three failure modes:

1. The model produced an oversized summary (often happens at high
   `summary_max_tokens` with chatty models)
2. `keep_recent_rounds` was too large for the log
3. The log was already mostly recent rounds, so there was nothing
   to compact

The new log is still applied. The assertion is non-fatal.

## Disabling compaction

Set `compaction.enabled` to `false` in the profile JSON, OR pick
the `off` preset in the setup wizard.

When disabled:

- The autocompact gate at `_begin_agent_turn` is a no-op
- The blocking buffer is **still** honored. The chat refuses to
  send a request that exceeds the API limit even when autocompact
  is off.
- Manual `/compact` still works
- Reactive PTL recovery still catches API rejections

## Testing

The relevant tests are:

| File | Coverage |
|------|----------|
| `tests/test_compaction_config.py` | CompactionConfig dataclass, JSON round trip, validation, lenient parsing |
| `tests/test_chat_compaction_scaling.py` | `_agent_budget()` percentage math at 8K / 50K / 128K / 200K / 262K / 1M / 2M windows |
| `tests/test_compaction_assertion.py` | post-compact size assertion |
| `tests/test_chat_autocompact_gate.py` | chat-layer autocompact gate (per-turn guard, in-flight guard, deferred resume, cancel) |
| `tests/test_chat_compaction_e2e.py` | edge cases (tiny window, huge window, disabled, invalid JSON) |
| `tests/test_wizard_compaction_snapshot.py` | wizard step visual rendering for each preset |
| `tests/test_config_menu_compaction_snapshot.py` | config menu compaction section visual rendering |
| `tests/test_agent_budget.py` | (existing) ContextBudget invariants, BudgetTracker, CircuitBreaker, RecompactChain |
| `tests/test_agent_compact.py` | (existing) compact() function, PTL retry, summary content |

The tests are hermetic. No live llama.cpp server is needed. The
`temp_config_dir` fixture isolates each test's config and registry
state. Mock clients implement the bare `stream_chat()` surface that
`compact()` and the chat both need.

## See also

- `src/successor/profiles/profile.py`: `CompactionConfig` + `Profile`
- `src/successor/agent/budget.py`: `ContextBudget`, `CircuitBreaker`,
  `RecompactChain`, `BudgetTracker`
- `src/successor/agent/compact.py`: the `compact()` function and the
  PTL retry loop
- `src/successor/agent/microcompact.py`: the cheap stale-result clearing
- `src/successor/chat.py`: `_agent_budget`,
  `_check_and_maybe_defer_for_autocompact`, `_handle_compact_cmd`
- `~/dev/agent-compaction-guide/`: the design reference this is
  modeled on (production-compaction.ts in particular)
- `~/dev/ai/free-code-main/src/services/compact/`: the parallel
  TypeScript implementation we cross-checked against
