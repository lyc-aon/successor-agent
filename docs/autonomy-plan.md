# Autonomy Plan

This document captures the current autonomy gap between Successor and
the local `free-code` reference, plus the phased implementation plan
for closing that gap without turning the harness into a prompt-shaped
slop pile.

The design rule is simple:

- keep autonomy observable
- keep control signals structured
- keep loop decisions deterministic enough to test and replay

## Deterministic Sources

The most useful 1:1 reference points in the local `free-code` tree are:

- `/home/lycaon/dev/ai/free-code-main/src/cli/print.ts`
  - proactive re-kick around the queued synthetic tick path
  - background-drain loop that keeps going while work is still live
- `/home/lycaon/dev/ai/free-code-main/src/query.ts`
  - token-budget continuation logic
- `/home/lycaon/dev/ai/free-code-main/src/constants/prompts.ts`
  - explicit task-management expectations
- `/home/lycaon/dev/ai/free-code-main/src/tools/TodoWriteTool/prompt.ts`
  - compact session task ledger guidance
- `/home/lycaon/dev/ai/free-code-main/src/tools/TaskCreateTool/prompt.ts`
  - proactive task creation guidance
- `/home/lycaon/dev/ai/free-code-main/src/tools/AgentTool/prompt.ts`
  - cheap delegation, "don't peek", and anti-race guidance
- `/home/lycaon/dev/ai/free-code-main/src/tools/AgentTool/built-in/verificationAgent.ts`
  - verification as a different control problem
- `/home/lycaon/dev/ai/free-code-main/src/skills/bundled/claudeInChrome.ts`
  - explicit browser-skill activation
- `/home/lycaon/dev/ai/free-code-main/src/utils/claudeInChrome/prompt.ts`
  - browser startup discipline and anti-rabbit-hole rules
- `/home/lycaon/dev/ai/free-code-main/src/services/toolUseSummary/toolUseSummaryGenerator.ts`
  - compact progress summaries after tool work
- `/home/lycaon/dev/ai/free-code-main/src/services/tools/toolOrchestration.ts`
  - deterministic concurrency partitioning

## Current Successor Gap

Successor already has strong primitives:

- a real multi-turn tool loop in `src/successor/chat.py`
- background subagents in `src/successor/subagents/`
- explicit browser / holonet / vision tools
- playback bundles and session traces

What it still lacks is orchestration:

1. No session-local task ledger.
   The model has to remember its own plan in free text.

2. Continuation is tool-result-driven, not progress-driven.
   Today the loop continues because a tool just finished. It does not
   continue because a structured task is still actively in progress.

3. Browser verification is still mostly prompt-steered.
   There is better browser guidance now, but not a true verification
   controller yet.

4. Subagent completions are observable but passive.
   They show up as notifications instead of becoming first-class loop
   fuel.

5. Progress summaries are still mostly implicit.
   Playback is good, but the runtime does not yet emit concise "what
   just happened" summaries the way `free-code` does.

## Principles

1. Do not paper over orchestration problems with more prose.
2. Prefer structured state over implicit assistant intent.
3. Keep new control signals visible in playback and trace logs.
4. Keep verification constraints mode-specific instead of global.
5. Avoid hidden autonomous loops that are hard to explain afterward.

## Phases

### Phase 1: Session Task Ledger

Goal: give the model an explicit place to track multi-step work and let
the runtime make one narrow continuation decision based on structured
task state.

Ship:

- internal native `task` tool
- session-local task ledger owned by `SuccessorChat`
- system-prompt section describing the current ledger
- one guarded continuation nudge when an `in_progress` task remains and
  the model stops too early
- trace events for ledger updates and continuation nudges
- rendered tool cards for task updates

Important constraint:

- continuation is only triggered when there is an explicit
  `in_progress` task
- the runtime gets at most one such nudge per user turn

Why this first:

- it is the closest deterministic analogue to `free-code`'s task/todo
  path
- it improves long-run organization without changing every other tool
- it is easy to test hermetically and easy to inspect in playback

### Phase 2: Browser Verification Controller

Goal: tighten browser QA / repro / polish turns without crippling normal
browser work.

Ship:

- verification-turn classifier
- browser-verifier runtime controller
- repeated-state / repeated-open / repeated-failure detection
- structured intervention events
- proof-oriented browser verification phases

Important constraint:

- constrain stalls, not actions
- browser operator remains broad; browser verifier gets stricter

### Phase 3: Progress Summaries And Subagent Loop Fuel

Goal: make long runs easier to follow and let completed background work
usefully steer the parent loop.

Ship:

- compact user-visible progress summaries after large tool batches
- concise structured "subagent completed, here is what changed" hints
- continuation reasons in the trace and playback viewer

### Phase 4: Broader Proactive Continuation

Goal: revisit true proactive re-kicks only after the task ledger and
verification controller are stable.

Possible future work:

- budget-aware continuation heuristics
- idle re-kick when structured work is still active
- stronger slot-aware coordination with subagents

## Testing Bar

Each phase must satisfy all of:

- unit tests for the new state/controller module
- chat-loop tests that cover continuation and stop conditions
- trace/playback assertions for new event types
- one supervised recorded E2E run
- one human-emulated visually inspected playback review

For Phase 1 specifically:

- task validation tests
- task-card serialization tests
- continuation-nudge tests
- "no nudge when no in-progress task" tests
- recorded multi-turn issue-desk or comparable local-app scenario

## Success Criteria

Phase 1 is successful when:

- the model uses the task ledger in long multi-step work
- the harness no longer stops early just because a turn ended in plain
  text while an explicit task is still in progress
- the loop does not spin forever because continuation is still bounded
- the full behavior is inspectable afterward through trace + playback
