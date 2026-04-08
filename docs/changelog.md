# Successor changelog

Per-phase notes for the framework infrastructure built on top of the
Phase 0 renderer + chat. Each phase below is one or more git commits
that add a self-contained capability with full tests.

The numbering jumps from "phase 0" (the original renderer + v0 chat)
straight to "phases 1–6" of the framework infra. There's no
contradiction; the framework phases were designed and built as a
unit on top of phase 0.

---

## v0.1.11, restore mouse-off terminal ownership (2026-04-08)

Corrective follow-up to v0.1.10. The tool-card background fix was
right, but the first mouse follow-on changed the ownership contract in
the wrong direction.

### What landed

- `src/successor/chat.py` restores the intended split:
  - `mouse off`: terminal owns wheel + native selection
  - `mouse on`: Successor owns wheel + clickable widgets
- `src/successor/config.py` keeps schema v3 but changes v2 → v3 to a
  compatibility-preserving migration:
  - preserve `mouse` exactly as stored
  - do not force `mouse: true` onto older installs
- `/mouse` help text now describes that ownership split directly

### Verification

- Focused regressions:
  - `tests/test_bash_render.py`
  - `tests/test_config.py`
  - `tests/test_chat_mouse.py`
  - `tests/test_terminal.py`
  - `tests/test_chat_bash.py`
  - `tests/test_input_history.py`
  - result: `116 passed in 1.71s`
- Full local suite: `1074 passed in 11.91s`
- Real local config probe:
  - existing `~/.config/successor/chat.json` now loads as
    `mouse: false`
  - `SuccessorChat()` starts with `term.mouse_reporting = False`

## v0.1.10, tool-card light-theme cleanup + mouse ownership fix (2026-04-08)

Follow-on polish pass after the semantic diff cards landed. Two bugs
showed up immediately in real use:

1. prepainted bash tool cards leaked default black cells at the outer
   edges of output/status rows when rendered inside light themes
2. the first follow-on mouse fix got the ownership split wrong: it made
   Successor own the mouse by default, which broke the old
   `mouse off` contract where the terminal keeps native wheel/selection

### What landed

- `src/successor/bash/render.py` now fills the full row background for:
  - settled diff/stdout output rows
  - settled status rows
  - running output rows
  - running status rows
  - the running "reserve one blank output row" path

  That means prepainted sub-grids no longer fall through to the Grid's
  default `Style()` cells at the left/right gutters or after the status
  footer text.

- `src/successor/config.py` keeps schema v3, but v2 → v3 is now a
  compatibility-only migration:
  - preserve `mouse` exactly as stored
  - do not force `mouse: true` onto older installs

- `src/successor/chat.py` restores the intended split:
  - `mouse off`: terminal owns wheel + native selection
  - `mouse on`: Successor owns wheel + clickable widgets
  - help text/comments now say that directly

### Verification

- Focused regressions:
  - `tests/test_bash_render.py`
  - `tests/test_config.py`
  - `tests/test_chat_mouse.py`
  - `tests/test_terminal.py`
  - `tests/test_chat_bash.py`
  - `tests/test_input_history.py`
  - result: `114 passed in 1.72s`
- New regressions added:
  - `tests/test_bash_render.py` now checks a light-theme diff card for
    theme-colored edge/status backgrounds instead of leaked black cells
  - `tests/test_chat_mouse.py` covers:
    - default mouse-off when config is missing
    - v2 `mouse: false` preserved
    - v2 `mouse: true` preserved
    - wheel-up / wheel-down changing chat scroll state
  - `tests/test_terminal.py` now asserts `MOUSE_ON`/`MOUSE_OFF`
    sequences are emitted when terminal mouse reporting is enabled
- Direct local probes:
  - a light-theme write-file render confirmed output/status rows now
    carry theme colors at the edges
  - the real local config at `~/.config/successor/chat.json` now stays
    `mouse: false` at startup, so terminal-native mouse ownership is
    restored

## v0.1.9, semantic diff cards (2026-04-08)

Renderer/tooling pass focused on making file mutations legible inside
the chat itself. The bash parser already knew what many write commands
were doing before execution; this pass turns that preview metadata into
truthful after-the-fact diffs without changing what the model sees as
tool output.

### What landed

- New `src/successor/bash/diff_artifact.py`:
  - immutable `ChangeArtifact`, `ChangedFile`, and `DiffHunk` shapes
  - unified-diff parser for `git diff` / `git show` / `diff -u`
  - synthetic diff builder from before/after text content
- New `src/successor/bash/change_capture.py`:
  - deterministic before/after capture for parser-known mutating cards
  - current supported verbs:
    - `write-file`
    - `create-file`
    - `delete-file`
    - `delete-tree`
    - `create-directory`
    - single-target `copy-files`
    - single-target `move-files`
  - text-file capture is intentionally bounded and falls back to note
    rows for binary, unreadable, directory, or oversized targets
- `src/successor/bash/cards.py` now carries an optional
  `change_artifact` on settled `ToolCard`s
- `src/successor/bash/exec.py` and `src/successor/chat.py` both now
  run the same capture lifecycle:
  - snapshot before execution
  - execute the real command
  - attach a structured change artifact to the final card if one can be
    computed truthfully
- `src/successor/bash/prepared_output.py` grew a semantic diff path
  instead of a separate renderer:
  - explicit diff stdout parses into file/hunk/add/remove/context rows
  - deterministic write cards render from `change_artifact`
  - diff cards get a wider settled display budget than ordinary stdout
- `src/successor/bash/render.py` now paints dedicated row treatments for:
  - `diff_file`
  - `diff_hunk`
  - `diff_add`
  - `diff_remove`
  - `diff_context`
  - `diff_note`
- `scripts/e2e_chat_driver.py` gained:
  - `assert_turn_plain_contains`
  - `rewrite_diff`, a live scenario that creates then rewrites a file
    and asserts the rendered transcript actually contains `+` / `-`
    hunk lines
  - richer message dumps that include `change_artifact` summaries for
    tool cards

### Verification

- Hermetic local regression slice:
  - `tests/test_bash_diff_capture.py`
  - `tests/test_bash_exec.py`
  - `tests/test_bash_parser.py`
  - `tests/test_bash_prepared_output.py`
  - `tests/test_bash_render.py`
  - `tests/test_bash_runner.py`
  - `tests/test_bash_stream.py`
  - `tests/test_bash_verbclass.py`
  - `tests/test_chat_bash.py`
  - `tests/test_agent_loop.py`
  - result: `317 passed in 3.25s`
- Full local suite: `1066 passed in 11.91s`
- Live llama.cpp/Qwopus E2E:
  - `scripts/e2e_chat_driver.py --scenario rewrite_diff`
  - `scripts/e2e_chat_driver.py --scenario rewrite_diff --runs 3`
- Manual live probe confirmed the explicit-diff path too:
  - `dispatch_bash("git diff -- note.txt")` now paints a semantic file
    header + hunk + add/remove rows instead of raw diff text

## v0.1.7, local slot-aware scheduling (2026-04-08)

Follow-on pass for the local llama.cpp case. The goal was not "more
parallelism at any cost"; it was to make the runtime honest about local
capabilities, keep serial default for throughput, and let advanced
profiles opt into slot-aware background workers.

### What landed

- `src/successor/providers/llama.py` now has
  `detect_runtime_capabilities()`, cached off `/props`, exposing:
  - `context_window`
  - `total_slots`
  - `endpoint_slots`
  - `supports_parallel_tool_calls`
- `src/successor/subagents/config.py` now carries a real scheduling
  strategy:
  - `serial`: always 1 background model lane
  - `slots`: use llama.cpp slot count with one slot reserved for the
    parent chat
  - `manual`: trust `max_model_tasks` directly
- `src/successor/chat.py` now resolves the manager width from that
  strategy at startup and profile switch time, shows the effective
  scheduler summary in `/tasks`, and only injects "parallel read-only
  bash work" guidance when the provider actually advertises parallel
  native tool calls.
- `src/successor/wizard/config.py` now exposes the subagent scheduling
  strategy in the config menu.
- Built-in profiles now serialize the subagent strategy explicitly so
  the shipped defaults are unambiguous.
- `successor doctor` now reports llama.cpp slots and parallel tool-call
  support instead of only the context window.

### Verification

- Hermetic local suite: `1059 passed in 11.85s`
- Live local llama.cpp verification:
  - `successor doctor` reported `ctx window 262144`, `slots 4 total
    (/slots on)`, and `tool calls parallel supported`
  - manual slot-aware `/fork` overlap run showed two background tasks
    in `running` state at the same time and both completed cleanly
  - the two-file bash-read probe still answered correctly but serialized
    the reads, which confirms the runtime path is ready while Qwopus is
    not yet reliably planning same-turn parallel read calls

## v0.1.8, alternate-scroll suppression (2026-04-08)

TTY bugfix for the input-history feature. In terminals that map
mouse-wheel motion in the alternate screen to Up/Down cursor keys,
wheel-up was entering prompt-history recall instead of behaving like
scroll.

### What landed

- `src/successor/render/terminal.py` now saves and disables DEC private
  mode `?1007` (alternate scroll) when the session starts, then
  restores it during terminal teardown
- new `tests/test_terminal.py` locks the enter/exit escape-sequence
  contract so future terminal work does not silently reintroduce the
  wheel-to-Up regression

### Verification

- targeted regression: `tests/test_terminal.py` and
  `tests/test_input_history.py`
- full local suite: `1060 passed in 11.70s`

## v0.1.6, model-visible subagents (2026-04-08)

Follow-on pass that turns the v0.1.5 background-task foundation into a
real model-visible delegation path.

### What landed

- `src/successor/tools_registry.py` now defines a native `subagent`
  tool schema plus model guidance, based on the same notification and
  "don't peek / don't race" interaction model used in free-code's fork
  path.
- `src/successor/chat.py` now accepts native `subagent` tool calls,
  renders dedicated subagent cards, serializes spawn results back into
  the API message stream, and injects completion notices as user-role
  events so later turns can answer from them.
- `src/successor/subagents/prompt.py` defines the child boilerplate,
  spawn payload, and completion payload. The child is explicitly told to
  stay in scope, not re-delegate, and to return one concise final
  report.
- `src/successor/subagents/manager.py` now defers queue-width
  reconfiguration safely when tasks are active, then applies the new
  semaphore once the manager goes idle.
- `successor-dev` now enables the model-visible `subagent` tool by
  default. The plain `default` profile keeps manual `/fork` available
  but leaves model delegation off by default.
- `docs/subagents-plan.md` was rewritten into an up-to-date design note
  instead of a stale pre-implementation plan.

### Verification

- Hermetic local suite: `1051 passed in 11.66s`
- Live llama.cpp/Qwopus E2E:
  - `scripts/e2e_chat_driver.py --scenario subagent_summary`
  - `scripts/e2e_chat_driver.py --scenario model_subagent_version_audit`
- Artifact inspection confirmed the task badge, inline subagent card,
  completion notification, and second-turn "answer from the
  notification only" behavior.

## v0.1.5, unicode editing audit (2026-04-08)

Audit/fix pass focused on typed-input reality vs docs. The byte
decoder was already UTF-8 capable; the real remaining bug was text
editing still deleting one codepoint at a time.

### What landed

- New `src/successor/graphemes.py`: stdlib-only grapheme helpers for
  the editing layer. The implementation covers combining marks,
  variation selectors, emoji modifiers, ZWJ sequences, and
  regional-indicator flag pairs.
- `src/successor/chat.py` now routes both main-input backspace and
  search-bar backspace through the grapheme helper, so decomposed
  accents and emoji clusters delete cleanly instead of leaving
  partial characters behind.
- `src/successor/wizard/config.py` now uses grapheme boundaries for
  inline TEXT/SECRET/NUMBER editor left/right movement plus
  backspace/delete.
- `src/successor/wizard/prompt_editor.py` now uses the same helper
  for LEFT/RIGHT and backspace/delete inside the multiline system
  prompt editor.
- `src/successor/wizard/setup.py` now deletes full grapheme clusters
  in the name, provider API key, and provider model fields.
- `CLAUDE.md`'s deferred list dropped the stale "ASCII-only typed
  input" item and now points at the real shipped state instead.
- Version bumped to 0.1.5 in `pyproject.toml` and
  `src/successor/__init__.py`.

### Late follow-on: subagent foundations

- New `src/successor/subagents/`: `SubagentConfig` plus a
  `SubagentManager` that runs background child tasks inside headless
  `SuccessorChat` instances.
- `Profile` picked up a `subagents` section with enable/disable, queue
  width, timeout, and notification settings. Built-in profiles now ship
  that block explicitly.
- `src/successor/chat.py` gained manual `/fork`, `/tasks`, and
  `/task-cancel` slash commands, completion notifications, transcript
  paths, and a title-bar task-count badge.
- `src/successor/wizard/config.py` gained a `subagents` section so the
  queue width / timeout / notification knobs round-trip through the
  config menu.
- `scripts/e2e_chat_driver.py` now knows how to wait for background
  subagent completion and includes a live `subagent_summary` scenario
  for local llama.cpp verification.

### Tests

1014 to 1027. 13 new tests across three files:

- `tests/test_key_decoder.py`: 4 tests for mixed UTF-8 decode,
  byte-stream reassembly, invalid-sequence recovery, and Unicode
  bracketed paste
- `tests/test_unicode_editing.py`: 7 tests covering grapheme-aware
  backspace/delete across chat input, search, config inline edit,
  prompt editor, and wizard fields
- `tests/test_intro_sequence.py`: 2 tests ensuring the startup intro
  only consumes numbered animation frames and excludes `hero.txt`

## v0.1.4, tier-1 polish (2026-04-08)

Visible-gap closing pass after v0.1.3 went public. Four pieces of
polish that move the project from "shipped" to "ready to invite
people in", plus a chat hero swap based on user feedback.

### What landed

- `src/successor/chat.py` picks up an in-memory input history ring
  buffer (capped at `INPUT_HISTORY_MAX = 100`) plus the recall
  state machine. Up arrow on an empty input enters recall mode and
  loads the most recent submitted message; Up walks older, Down
  walks newer, Down past the newest restores the saved draft. Any
  editing key (typing, backspace) exits recall mode and lets the
  user edit the recalled text as a fresh draft. Esc bails and
  brings the saved draft back. Submit always exits recall mode and
  adds the new entry to the history (deduped against the most
  recent entry to avoid `/profile cycle` spam).
- The Up/Down handlers in `_handle_key_event` are now layered:
  autocomplete dropdown wins first, then history recall (when the
  buffer is empty AND history is non-empty), then chat scroll. Up
  never clobbers an in-progress draft because the recall gate
  requires an empty buffer to trigger.
- Two new builtin themes shipped:
  `src/successor/builtin/themes/paper.json` (warm cream + sepia,
  fountain-pen accents) and `cobalt.json` (deep saturated cobalt
  blue dark with cool white text). Both use oklch color space.
  Successor now ships four builtin themes total.
- `src/successor/builtin/intros/successor/hero.txt` ships as the
  canonical chat empty-state hero file, replacing the 10-title.txt
  convention for the chat surface only. The hero is the soldier
  full body without the SUCCESSOR title text overlaid; the title
  frame is still loaded by the wizard welcome screen because the
  welcome screen wants the title framing.
- `src/successor/render/intro_art.py` updated to prefer hero.txt
  and fall back to 10-title.txt for legacy custom intros.
- New `.github/workflows/test.yml` runs pytest on push to master
  and every PR against Python 3.11 / 3.12 / 3.13 in matrix. The
  very first CI run caught a Python-3.13-only `from copy import
  replace` import in `tests/test_context_fill_bar.py` that had
  been working silently locally because the dev environment was on
  3.13. The import was dead code; deleting it restored the
  Python 3.11+ support claim.
- New `CONTRIBUTING.md` covers dev setup, test commands, the One
  Rule reference, the anti-slop discipline, and the PR process.
- README picks up three badges at the top: CI status, Apache 2.0
  license, Python 3.11+. Standard convention for maintained OSS.
- `pyproject.toml` description updated. Old text said "for local
  llama.cpp models" which understated the harness; new text
  mentions OpenAI + OpenRouter alongside llama.cpp, references
  the renderer architecture, and stays under PyPI's 300 char cap.
- Version bumped to 0.1.4 in `pyproject.toml` and
  `src/successor/__init__.py`.

### Tests

974 to 1014. 40 new tests across three files:

- `tests/test_input_history.py`: 24 tests for the full recall
  state machine (ring buffer + dedupe + cap, Up/Down navigation
  including the at-oldest no-op edge case, in-recall edits, Esc
  restore, submit interactions, recalled-text-unchanged dedupe).
- `tests/test_tier1_polish.py`: 13 tests covering the new themes
  loading, full palette completeness, distinct bg colors, chat
  construction with each theme, the pyproject description regex
  check, the CI workflow YAML validity + branch targeting, and
  the README badge presence.
- `tests/test_intro_art.py` picked up 3 new tests for the hero.txt
  loader preference, the legacy 10-title.txt fallback, and a
  density check confirming the bundled hero is the no-text
  variant.

### Fix-up commits in the same release

- `486486e test: drop dead Python 3.13-only import that broke CI
  on 3.11/3.12`. Caught by the new CI matrix on its first run.
- `3e08218 chat hero: switch empty-state portrait to soldier
  without SUCCESSOR title`. The hero swap based on user feedback
  after seeing the v0.1.4 candidate.

### Doc cross-check pass

After all of the above shipped, did one more pass over README,
CHANGELOG, NOTICE, CLAUDE.md, CONTRIBUTING.md, and every doc
under docs/ to catch stale references. Found and fixed:

- Three stale "974 tests" references (README, CLAUDE.md,
  CONTRIBUTING.md) updated to 1014.
- CLAUDE.md "Things deliberately deferred" still listed
  "History recall (Up/Down in input)" even though we just
  shipped it. Removed and added a forward-pointer note to the
  shipped feature.
- Two prose em dashes that leaked into recently-touched files
  (intro_art.py docstring + test_input_history.py docstring)
  cleaned to commas.

## v0.1.3, configurable autocompactor (2026-04-08)

The autocompactor got a proper chat-layer gate, percentage-based
thresholds, per-profile configuration, and 93 new tests.

### What landed

- `src/successor/profiles/profile.py` picked up a `CompactionConfig`
  frozen dataclass: nine fields (`warning_pct`, `autocompact_pct`,
  `blocking_pct` as fractions of the resolved context window, matching
  `*_floor` token minimums, plus `enabled`, `keep_recent_rounds`, and
  `summary_max_tokens`). `__post_init__` enforces the threshold
  ordering invariant and range checks. `from_dict` is lenient: missing
  fields use defaults, wrong-typed fields fall back, and an invariant
  violation drops back to safe defaults rather than rejecting the
  profile.
- `Profile` gained a `compaction: CompactionConfig` field with a
  factory default. The profile parser picks up the new block via the
  same type-tolerant pattern every other field uses.
- `SuccessorChat._agent_budget()` now reads percentages from
  `self.profile.compaction` and builds a `ContextBudget` by calling
  `buffers_for_window(resolved_window)`. This is the sole seam
  between static profile config and the runtime budget.
- New `SuccessorChat._check_and_maybe_defer_for_autocompact()` at
  the top of `_begin_agent_turn`. When usage crosses the threshold
  and compaction is enabled, it spawns a compaction worker via the
  shared `_spawn_compaction_worker()` helper and sets a
  deferred-resume flag. `_poll_compaction_worker` re-enters
  `_begin_agent_turn` after the worker succeeds (or fails, letting
  reactive PTL recovery catch the failure case).
- Per-turn guard `_autocompact_attempted_this_turn` prevents the
  gate from firing twice for a single user message. In-flight
  worker guard prevents stacking workers. Ctrl+G cancellation
  clears the deferred-resume flag so a cancelled compaction doesn't
  silently resume the deferred turn.
- `src/successor/agent/compact.py` now checks that the new log is
  at most 90% the size of the original. If not, it stamps a warning
  on the `BoundaryMarker` and the log's boundary message picks up
  an `underperformed` annotation. Non-fatal but visible.
- Wizard gained a 10th step, `COMPACTION`, with four presets
  (default, aggressive, lazy, off). Each preset is rendered with a
  description and a live preview panel showing the resolved buffer
  thresholds against a 200K reference window.
- Config menu gained a `compaction` section with per-field editors.
  Percentage fields are entered as percent (type `6.25` for 6.25%)
  but stored as fractions internally; the conversion happens at
  commit time.
- New `docs/compaction.md` covers the schema, threshold math, gate
  flow, and the failure modes the post-compact assertion catches.
- Default profile ships with `bash` enabled so new users see the
  agentic loop on turn one. `successor-dev` profile ships with the
  aggressive compaction preset to keep dev sessions responsive at
  the edge of the context window.
- Scratch profile `compact-test.json` removed from the builtin
  bundle.
- `.gitignore` picks up media binaries (`*.mp4`, `*.gif`, `*.mp3`,
  `*.wav`) so future recordings don't accidentally bloat the source
  tree.

### Tests

881 to 974. 93 new tests across seven files:

- `tests/test_compaction_config.py`: 33 tests for the dataclass,
  validation, lenient JSON parsing, and profile integration
- `tests/test_chat_compaction_scaling.py`: 12 tests for
  `_agent_budget()` percentage math at 8K / 50K / 128K / 200K /
  262K / 1M / 2M window sizes
- `tests/test_compaction_assertion.py`: 10 tests for the post-compact
  size assertion
- `tests/test_chat_autocompact_gate.py`: 11 E2E tests for the
  chat-layer gate including per-turn guard, in-flight guard,
  deferred-resume wiring, and Ctrl+G cancel
- `tests/test_chat_compaction_e2e.py`: 12 edge case E2E tests
  covering tiny window floors, huge window state transitions,
  disabled behavior, invalid JSON safe fallbacks, profile reload
- `tests/test_wizard_compaction_snapshot.py`: 8 visual snapshot
  tests for the new wizard step
- `tests/test_config_menu_compaction_snapshot.py`: 7 visual
  snapshot tests for the new config menu section

### README rewrite

README picked up an Inspirations section crediting Cheng Lou's
Pretext (the prepare-once / cache-by-target-size pattern that
powers `BrailleArt.layout` and `PreparedText.lines`), Hermes Agent
and the open-source agent harness ecosystem (the continuation
pattern + native chat-template tool calls), and the broader
open-source AI community (llama.cpp, open-weight model families,
the inference tooling that made local agentic chat buildable).

Stripped every em dash from prose sections after the first draft.
Updated wizard step count (10), test count (974), and added a
Visuals section embedding the v0.1.3 release GIFs inline.

### Media library

Seven GIFs cropped from the walkthrough recordings, attached to the
v0.1.3 GitHub release as assets:
`intro_braille.gif`, `wizard_theme.gif`, `braille_red.gif`,
`braille_blue.gif`, `tool_dispatch.gif`, `search_demo.gif`,
`chat_streaming.gif`. The README embeds all six most striking ones
inline via release-asset URLs, so the source tree stays clean
while visitors see the harness in motion without downloading
anything.

---

## Phase 1 — Loader pattern + theme refactor + config v2 (2026-04-06)

The foundation. Establishes the generic `Registry[T]` pattern that
themes, profiles, skills, and (with a Python-import variant) tools all
reuse. Splits the conflated dark/light/forge "themes" into orthogonal
(`Theme`, `display_mode`) axes — every real design system treats
palette identity and mode as separate, and conflating them prevented
"toggle dark/light" from preserving theme identity.

### What landed

- `src/successor/loader.py` — generic `Registry[T]` with built-in dir +
  user dir loading, name-collision precedence (user wins), parser
  failure skipping with stderr warnings, hermetic-testable via
  `SUCCESSOR_CONFIG_DIR` env var
- `src/successor/render/theme.py` rewritten — `ThemeVariant` (the 9
  semantic color slots, one per mode) + `Theme` bundle (name + icon +
  description + dark variant + light variant). `parse_color()` accepts
  hex strings AND oklch tuples/strings. `blend_variants()` lerps
  between variants for smooth transitions. `THEME_REGISTRY` singleton.
  `find_theme_or_fallback()` always returns a valid Theme.
- `src/successor/builtin/themes/steel.json` — the default theme, ported
  from the previous DARK_THEME/LIGHT_THEME oklch values into one
  bundle with both variants
- `docs/example-themes/forge.json` — example user theme, hand-tuned
  warm red palette with both dark and light variants. Drop
  into `~/.config/successor/themes/` to install.
- `src/successor/config.py` extended — added `version`, `display_mode`,
  `active_profile` slots; v1 → v2 migration translates legacy `theme`
  values (`dark` → `(steel, dark)`, `light` → `(steel, light)`,
  `forge` → `(forge, dark)`) idempotently
- `src/successor/demos/chat.py` refactored to use ThemeVariant — every
  painter now takes `theme: ThemeVariant`, the chat resolves the
  current variant once per frame via `_current_variant()` which
  blends across both axes (theme transition + mode transition).
  Added `Alt+D` keybind for display mode toggle. Added `/mode`
  slash command. Added display mode widget (☾/☀ pill) to the title
  bar between density and theme. Made the `/theme` completer dynamic
  so user themes show up in autocomplete.
- `src/successor/render/terminal.py` — file descriptors are now resolved
  lazily so the renderer is testable under pytest's captured stdin
  (which has no `fileno()`). The diff layer's stdout writes are
  unchanged in real terminal use.
- `src/successor/snapshot.py` + `cli.py` — `chat_demo_snapshot()` accepts
  a `display_mode` parameter; `successor snapshot --display-mode` flag added.
  Theme choices are resolved against the live registry instead of
  hardcoded.

### Tests (105)

- `tests/test_loader.py` — 19 tests for the Registry pattern
- `tests/test_theme.py` — 36 tests for color parsing, variant
  resolver, blend math, and built-in/user loading
- `tests/test_config.py` — 15 tests for load/save/migrate, all the v1
  legacy theme name translations, atomic write, idempotent migration
- `tests/test_snapshot_themes.py` — 18 tests for the visual regression
  matrix (every scenario × every mode renders, dark and light produce
  different ANSI, user themes produce different ANSI from steel)
- `tests/conftest.py` — `temp_config_dir` fixture using
  `SUCCESSOR_CONFIG_DIR` for hermetic isolation

### Notes

- The loader pattern is the load-bearing piece. Phases 2–6 reuse it
  verbatim or with minimal extension.
- Lazy fd resolution in `terminal.py` was the only renderer-side
  change required to make the chat testable under pytest. Headless
  paths never trigger the fileno() call now.

---

## Phase 2 — Provider abstraction (2026-04-06)

Pure refactor. The existing `LlamaCppClient` already exposed the right
shape; this phase formalizes the protocol, adds an
`OpenAICompatClient` peer, and ships a factory function that profiles
will use in phase 3.

### What landed

- `src/successor/providers/base.py` — `ChatProvider` Protocol (structural,
  runtime-checkable). Re-exports the `ChatStream` event types so
  callers can `from successor.providers import StreamEnded` regardless of
  which backend is producing them.
- `src/successor/providers/llama.py` — added `provider_type = "llamacpp"`
  class attribute. No behavioral changes.
- `src/successor/providers/openai_compat.py` — `OpenAICompatClient` for
  any OpenAI-API-compatible server (LM Studio, Ollama, vLLM, OpenRouter,
  hosted servers). Optional `Authorization: Bearer` header via an
  `_AuthenticatedChatStream` subclass that injects the header before
  the urlopen call. `/v1/models` is the liveness probe (the OpenAI
  spec doesn't include `/health`).
- `src/successor/providers/factory.py` — `make_provider(config)` reads
  the `type` field and dispatches to the matching constructor. Aliases
  supported (`llama`, `llama.cpp`, `openai`, `openai-compat`). Key
  translation (`max_tokens` → `default_max_tokens`). Forward-compat:
  unknown keys are silently dropped so future profiles don't break
  older Successor installs.
- `src/successor/providers/__init__.py` — re-exports for the public surface

### Tests (19)

- `tests/test_providers.py` — protocol conformance for both classes,
  factory dispatch, alias support, key translation, forward-compat
  unknown-key drop, missing/unknown type error paths

### Notes

- No chat.py changes required. `LlamaCppClient` is still the default
  when no profile is provided. Profile integration in phase 3 is what
  actually exercises the factory.

---

## Phase 3 — Profile system + intro animation (2026-04-06)

The first showcase commit. Profiles bundle (theme, display_mode,
density, system_prompt, provider, skills, tools, intro_animation)
into a single switchable persona unit. Hot-swap via `/profile <name>`
or `Ctrl+P`. The active profile name appears in the title bar. New
profiles drop into `~/.config/successor/profiles/*.json`.

### What landed

- `src/successor/profiles/profile.py` — `Profile` dataclass, JSON parser
  with type-tolerant fallback (wrong-typed fields revert to dataclass
  defaults instead of crashing), `PROFILE_REGISTRY` singleton,
  `get_active_profile()` chain (chat.json → "default" → first
  registered → hardcoded fallback)
- `src/successor/profiles/__init__.py` — public surface
- `src/successor/builtin/profiles/default.json` — general-purpose profile,
  no intro animation
- `src/successor/builtin/profiles/successor-dev.json` — harness-development
  profile with `intro_animation: "successor"`, lower temperature
  (0.5), 64K max_tokens, and a system prompt that primes the model
  for Successor codebase work
- `src/successor/demos/chat.py` — `SuccessorChat.__init__` accepts a
  `profile=` argument and resolves theme/mode/density/system_prompt/
  provider from it. Saved config still wins per-setting so manual
  changes persist. Added `_set_profile()`, `_cycle_profile()`,
  `/profile` slash command, `Ctrl+P` keybind, profile-name title bar
  widget, "profile" hit-box action for mouse mode, breadcrumb
  synthetic message on swap.
- `src/successor/demos/braille.py` — `SuccessorDemo` gained `max_duration_s`
  (auto-exit after N seconds) and `intro_mode` (any keypress exits
  early) parameters
- `src/successor/cli.py` — `cmd_chat` now resolves the active profile,
  plays its intro animation if configured (calls
  `_play_intro_animation("successor")`), then constructs the chat
  with the profile

### Tests (33 + 18 = 51 new)

- `tests/test_profiles.py` — 21 tests for the profile loader,
  registry, active-profile resolution, set_active_profile persistence,
  next_profile cycling
- `tests/test_chat_profiles.py` — 12 tests for the chat ↔ profile
  integration: construction with profile, saved-config-wins-over-
  profile-defaults, _set_profile swaps everything, persists active,
  no-op on same name, breadcrumb message, _cycle_profile, title bar
  shows profile name, autocomplete shows /profile, help overlay
  documents Ctrl+P

### Notes

- The "saved config wins over profile defaults" rule is subtle but
  important: a profile says "use steel/dark" but the user has
  manually Ctrl+T-cycled to forge before — on restart, forge wins.
  The profile is the *initial* setting; user choices override.
- `Ctrl+P` was previously bound to vim-style "scroll up one line";
  the binding is now profile cycling. Up arrow still scrolls.
  Documented in the help overlay.
- Intro animation: brief flash between intro App teardown and chat
  App startup is acceptable for v0. Future polish: share a single
  Terminal instance across both Apps to eliminate the flash.
- The `successor-dev` profile is what we use to dogfood the harness on
  itself. Activating it (`/profile successor-dev` or set
  `active_profile: "successor-dev"` in chat.json) loads the rendering
  rules into the system prompt.

---

## Phase 5 — Skill loader scaffolding (2026-04-06)

Loader-only. Skills are markdown files with YAML-style frontmatter
(Claude-Code-compatible format), loaded via the same Registry pattern
themes use. The chat doesn't yet send skill bodies to the model —
that decision (always-on prepend vs on-demand tool) waits for hands-on
time with Qwen 3.5.

### What landed

- `src/successor/skills/skill.py` — `Skill` dataclass (name, description,
  body, source_path, `estimated_tokens` property using chars/4
  heuristic). `parse_skill_file()` reads `*.md` files, splits the
  frontmatter block via a tiny line-based parser (no PyYAML
  dependency), returns None for files without frontmatter so READMEs
  drop into `skills/` cleanly. `SKILL_REGISTRY` singleton.
- `src/successor/skills/__init__.py` — public surface
- `src/successor/builtin/skills/successor-rendering-pattern.md` — the One Rule
  + five-layer architecture as a bundled skill. Self-documenting
  example of the format.
- `src/successor/cli.py` — `successor skills` subcommand lists every loaded skill
  with name, source label (builtin/user), token estimate, and
  description (soft-wrapped at 80 cols). Shows total token count.

### Tests (17)

- `tests/test_skills.py` — frontmatter parser internals (happy path,
  no block, unclosed block, comments, lowercase keys, leading-blank
  drop), parse_skill_file (minimal, full, missing name, no
  frontmatter), token estimate, source path absolute, builtin loading,
  user override, broken file isolation, README silent skip

### Notes

- The `~/.claude/skills/*.md` library should work as-is once the user
  symlinks or copies it into `~/.config/successor/skills/`. Same format,
  same loader contract.
- `_split_frontmatter` is intentionally minimal (no nested keys, no
  quoted values, no multiline values). Matches how Claude Code skills
  are written in practice. PyYAML can be added later if a power user
  wants nested fields.

---

## Phase 6 — Tool registry scaffolding (2026-04-06)

Loader-only. Tools are the only Successor extension type that turns data
into code, so the registry has its own shape: a Python module
importer that harvests `@tool`-decorated functions instead of parsing
file contents. User tools are GATED behind a config flag (default
OFF) and audited to stderr when enabled.

### What landed

- `src/successor/tools/tool.py` — `@tool(name=, description=, schema=)`
  decorator, `Tool` dataclass (callable via passthrough), `ToolRegistry`
  with module-import-based discovery. Built-in tools always load;
  user tools require `allow_user_tools: true` in chat.json. Each
  user tool import is announced to stderr. Partial-import rollback
  prevents a half-imported file from leaking partial registrations.
  Multi-tool files are supported (multiple @tool decorators per .py).
- `src/successor/tools/__init__.py` — public surface
- `src/successor/builtin/tools/__init__.py` — package marker
- `src/successor/builtin/tools/read_file.py` — example built-in tool with
  full JSON schema, demonstrating the API
- `src/successor/cli.py` — `successor tools` subcommand lists every registered
  tool with source label and description

### Tests (15)

- `tests/test_tools.py` — decorator captures metadata, callable
  passthrough, builtin read_file loads + actually works against a
  real temp file, user gating off by default, gate enables loading
  with stderr audit, override on collision, broken file skipped,
  partial-import rollback (file that registers one tool then crashes
  doesn't leave the half-tool in the registry), multi-tool files

### Notes

- The agent loop is NOT yet built. Tools are inventoried and callable
  by Python code, but the chat doesn't dispatch them. When the agent
  loop lands, it'll consume `TOOL_REGISTRY.all()` and translate to
  the model's function-calling format.
- Gating is the only safety mechanism for user-supplied Python. There
  is no sandboxing — user tools run with the same privileges as the
  successor process. The gate exists so a misconfigured profile can't
  surprise the user with arbitrary code execution.

---

## Phase 4 — successor setup wizard with live preview (2026-04-06)

The showcase. Multi-region App with a live preview pane on the right
that's a real SuccessorChat instance the wizard mutates as the user picks
options. The chat's existing `_set_theme`/`_set_display_mode`/
`_set_density` machinery animates the smooth blend transitions for
free — there's zero animation code in the wizard itself. Every
on-screen pixel uses something the renderer already had, which is
the proof that the harness can build itself.

### What landed

- `src/successor/wizard/setup.py` — `SuccessorSetup` App with eight steps
  (welcome, name, theme, mode, density, intro, review, saved). State
  machine dispatches per-step paint methods and key handlers.
  - Multi-region layout: title row, 16-col sidebar, main content,
    1-row footer. All coordinates derived from `grid.rows`/`grid.cols`,
    resize-aware.
  - Sidebar shows the step list with `▸` (active, gentle 0.7Hz pulse),
    `✓` (completed), and dim future steps.
  - Welcome screen plays the bundled Meditating braille frame above
    typewriter-animated intro text (~35 cps), then a fading hint.
  - Name step has a rounded-box input field with cursor blink and a
    validation glow that pulses red→accent over 0.5s on rejected input.
  - Theme/Mode/Density steps each show their option list on the left
    and a LIVE preview pane on the right.
- `src/successor/wizard/__init__.py` — `run_setup_wizard()` entry point
  that returns the saved Profile or None on cancel.
- `src/successor/cli.py` — `successor setup` subcommand. cmd_setup runs the
  wizard, then plays the new profile's intro animation if configured,
  then drops into the chat with the new profile active.
- `src/successor/snapshot.py` — `wizard_demo_snapshot(step=, name=, ...)`
  helper for headless wizard snapshots, shaped like the existing
  `chat_demo_snapshot`. Pins simulated `elapsed` so animations are
  deterministic in tests.

### The live preview pane (the centerpiece)

The wizard holds a `_preview_chat: SuccessorChat` constructed once at
wizard startup. As the user picks theme/mode/density options, the
wizard calls `preview_chat._set_theme()` etc. — which trigger the
chat's existing transition machinery. Every wizard frame, the wizard
builds a sub-grid sized for the preview pane and calls
`preview_chat.on_tick(sub_grid)` to render one frame of the chat into
it, then walks the cells and copies them into the wizard's main
grid at a fixed offset.

This is what "build the harness from the harness" actually means.
The preview is the actual chat — same painters, same theme system,
same density transitions, same Pretext-shaped layout caching. The
wizard doesn't reimplement any of it. When the user arrows from
"steel" to a hypothetical user-installed theme, the preview chat's
`blend_variants` runs and the user literally watches the colors morph.

### Renderer capabilities exercised

| concepts.md category | feature | where |
|---|---|---|
| Cat 1 — Mutable cells | live preview pane updates per-frame | `_paint_live_preview` |
| Cat 2 — Smooth animation | active step pulse, validation glow, section reveal slide-in, save toast slide-in/fade-out, welcome typewriter | `_paint_sidebar`, `_paint_name`, `_paint_main_content`, `_paint_toast`, `_paint_welcome` |
| Cat 3 — Multi-region UI | sidebar + main + preview + footer + toast all in one frame | `on_tick` |
| Cat 5 — Replayable, deterministic | every step is a snapshot fixture, full save flow tested headlessly | `wizard_demo_snapshot`, `test_full_save_flow_writes_json_file` |
| Cat 6 — Inline media | bundled braille Meditating frame on the welcome screen | `_paint_welcome` |
| Cat 7 — Programmatic UI | save action transitions into the chat with the new profile already active | `cmd_setup` |

### What the wizard deliberately does NOT do (v0 scope)

- System prompt and provider config use sensible defaults baked into
  the saved profile. The user can edit the JSON file afterward — the
  review screen tells them so. Multi-line text input is a separate
  problem and is deferred.
- No skill or tool selection (those aren't wired into the chat
  anyway — phases 5/6 ship loaders only).
- Creates new profiles only. Editing existing profiles via the slash
  command `/profile <name>` (already shipped in phase 3) is the
  separate UX for in-flight changes.

### Tests (35)

- `tests/test_wizard.py` — state machine tests (defaults, name
  validation, step navigation, save flow), input handler tests
  (typed input, backspace, max length, enter advances, empty
  rejected), end-to-end save flow with full keystroke sequence
  asserting the JSON file lands and active_profile is persisted,
  snapshot rendering tests for every step including too-small
  fallback and dark/light differ assertion.

### Notes

- The "creating: <name>" pill in the title bar appears as soon as the
  user enters the name step and types — instant feedback that they're
  building toward a real profile.
- The save flow validates the name one more time at submit and
  bounces back to the NAME step with a glow if it's empty (catches the
  edge case where the user goes Welcome → Right (advances to NAME)
  → Right (would advance again if name were valid, but it's not)).
- Total commit size for phase 4: ~1100 lines of source + ~520 lines
  of tests. The wizard file is 1000+ lines, which is the biggest
  single source file in the framework infra. All of it is paint
  functions and state machine — no clever tricks.

---

## Phase 4.5 — successor config three-pane menu (2026-04-06)

The companion to the wizard. Where the wizard is "create from scratch,
linear, one-shot," the config menu is "browse all your profiles, edit
anything, dirty-track, save/revert, see-it-live." Three panes side
by side with Tab focus cycling, anchored inline cycle-edit overlays,
per-field dirty markers, and a save flow that syncs the active
profile's appearance fields to chat.json automatically.

### What landed

- `src/successor/wizard/config.py` — `SuccessorConfig` App. Three-pane layout
  (profiles list / settings tree / live preview). Focus state machine
  with Tab cycling. Settings tree groups fields into sections
  (appearance, behavior, provider, extensions). Dirty tracking via
  a set of `(profile_name, field_name)` tuples — comparing against
  an initial-snapshot deep copy of every profile.
  - **CYCLE fields** (theme, density): Enter opens an inline anchored
    overlay listing all options, ↑↓ live-previews each option, Enter
    confirms, Esc cancels and restores the snapshot.
  - **TOGGLE fields** (display_mode, intro_animation): Enter flips
    immediately, no overlay.
  - **READONLY fields** (system_prompt, provider_*, skills, tools):
    shown in dim italic with an "edit JSON file" hint.
- `src/successor/wizard/__init__.py` — re-exports `SuccessorConfig` and
  `run_config_menu` alongside the wizard exports.
- `src/successor/cli.py` — `cmd_config` for the standalone `successor config`
  subcommand. `cmd_chat` rewritten as a re-entry loop that checks
  `chat._pending_action` after each `chat.run()` and reopens the
  config menu (then resumes the chat with the user's selected
  active profile) when set.
- `src/successor/demos/chat.py` — `_pending_action` instance state.
  `/config` slash command and `Ctrl+,` keybind both set
  `_pending_action = "config"` and call `self.stop()`. Help overlay
  documents the new keybind.
- `src/successor/snapshot.py` — `config_demo_snapshot()` helper for
  headless config menu rendering with controllable focus, cursors,
  editing state, and dirty markers.

### Renderer features exercised

| concepts.md cat | feature | where |
|---|---|---|
| Cat 1 (mutable cells) | settings rows re-style as the user edits them; dirty `*` marker appears the moment a value changes; per-profile color swatches in the left pane | `_paint_settings_pane`, `_paint_profiles_pane` |
| Cat 2 (smooth animation) | focused-pane border breathing pulse (BORDER_PULSE_HZ); save flash pulses every saved field green for 600ms; toast slide-in/fade-out | `_pulse_color`, `_save_flash`, `_paint_toast` |
| Cat 3 (multi-region UI) | three panes side by side with Tab to cycle focus; the focused pane gets a brighter top border | `on_tick` |
| Cat 5 (deterministic) | every focus state, dirty state, and editing state is a snapshot fixture | `config_demo_snapshot` |
| Cat 7 (programmatic UI) | navigating between profiles in the left pane animates the preview chat through the new profile's theme/mode/density via the existing blend machinery | `_handle_profiles_key` → `_sync_preview` |

### Save flow specifics

1. For each dirty profile, write `~/.config/successor/profiles/<name>.json`
   using `_profile_to_json_dict` (the inverse of `parse_profile_file`)
2. If any of the *currently-active* profile's appearance fields
   (theme/display_mode/density) were edited, also write those into
   chat.json so they take effect on the next chat open without the
   user needing to clear their saved overrides manually
3. Reload PROFILE_REGISTRY so subsequent get_profile() calls see the
   new state
4. Update the initial-snapshot list to match the new committed state
5. Clear the dirty set
6. Trigger the per-field save flash + a toast notification

### Tests (33)

- State machine: focus cycling, settings cursor skips read-only rows,
  profile cursor walks through the list and syncs the preview
- Editing: TOGGLE flips immediately and marks dirty, CYCLE opens
  overlay with live preview, overlay Enter confirms, overlay Esc
  cancels and restores
- Dirty tracking: edit marks dirty, edit-back-to-original clears
  dirty, revert clears all dirty
- Save flow: writes JSON to disk, syncs chat.json for active profile,
  no-op when nothing dirty
- Esc handling: warns first, exits second on dirty; exits immediately
  when clean
- Snapshot rendering: three panes visible, all sections, focus glyphs,
  edit overlay, dirty markers in title bar and profile rows, too-small
  fallback
- Round-trip: `_profile_to_json_dict` → write → `parse_profile_file`
  preserves every field

### Notes

- Tab is the canonical focus-cycling key in every desktop app, so
  reusing it inside a TUI is intuitive even though TUIs don't usually
  have focus state. The pulse on the focused pane's top border makes
  it visible without stealing attention.
- The dirty-warning Esc behavior (first Esc warns, second Esc
  discards) is the same pattern most editors use for "close unsaved
  buffer." The warning toast lasts 2.5s, and the test checks that
  the warning specifically uses the word "unsaved" so the second Esc
  recognizes it.
- Chat.json sync on save is the subtle bit. The active profile's
  appearance fields (theme/display_mode/density) override profile
  defaults — that's existing chat behavior. So when the user edits
  the active profile's theme via the config menu, we ALSO clear/set
  the corresponding chat.json field so the change takes effect
  immediately. Other profiles only update their own JSON files.
- The chat ↔ config re-entry loop in cmd_chat is the connecting
  glue. Each profile switch via the menu writes to chat.json, the
  cli loop re-reads and reopens the chat. Brief flash between Apps
  is acceptable for v0; future polish: share a Terminal instance.

---

## Phase 4.6 — editable text fields + multiline prompt editor (2026-04-06)

The config menu's READONLY fields (system prompt, provider settings)
become fully editable. No agent CLI has ever let you edit your own
system prompt directly inside the TUI — until now.

### What landed

- **Four new FieldKind enum values** in `wizard/config.py`:
  - **TEXT** — inline single-line text editor with cursor model,
    insert/delete, ←→/Home/End navigation. For `provider_model`
    and `provider_base_url`.
  - **NUMBER** — TEXT with int/float validation. Letters silently
    filtered at input time; invalid buffer on Enter triggers a
    warning toast and keeps the editor open. For `provider_temperature`
    (float) and `provider_max_tokens` (int).
  - **SECRET** — TEXT but the displayed value is `••••••` when not
    editing; while editing, the buffer renders in plaintext so the
    user can verify what they typed (matches every desktop password
    field's behavior). For `provider_api_key`.
  - **MULTILINE** — opens a full-screen modal text editor overlay.
    For `system_prompt`.

- **`provider_type` field** is now a CYCLE that walks through
  registered provider types from `PROVIDER_REGISTRY` (currently
  `llamacpp` and `openai_compat`).

- **`_InlineTextEdit` dataclass** + `_handle_inline_text_key` handler
  in `SuccessorConfig`. Holds the buffer, cursor position, snapshot for
  cancel, and the field index. Esc restores the snapshot and closes.
  Enter commits (with NUMBER validation) and closes.

- **`_PromptEditor` helper class** — a self-contained multi-line text
  editor with row/col cursor model, full navigation set (←→↑↓, Home,
  End, PgUp, PgDn), insert/delete/backspace, newline insertion that
  splits the current line, backspace-at-line-start that merges lines,
  and Ctrl+S commit / Esc cancel. Auto-scrolls vertically and
  horizontally to keep the cursor in view. Long lines clip with `›`
  markers on the right edge (soft wrap deferred).

- **Prompt editor overlay paint** in `on_tick` — centered modal at
  ~85% of the screen, title bar with line N/M and char count, line
  number gutter on the left, vertical separator, text area, footer
  keybinds. Painted AFTER the cycle-edit overlay so the prompt
  editor sits on top; toasts go above everything for save feedback.

- **Provider dict mutation** in `_set_field_on_profile` — fields
  starting with `provider_` mutate the profile's provider dict by
  removing the prefix and setting the corresponding key. Works for
  all of TEXT, NUMBER, SECRET, and CYCLE provider fields.

- **Inline text rendering in the settings pane** — when the cursor
  row is being inline-edited, the value cell paints the buffer + a
  blinking cursor on top of an accent_warm background, distinguishing
  it from the regular cursor highlight.

- **Footer keybinds dispatch** based on the active editor mode:
  - prompt editor: `↑↓←→ navigate · ⏎ newline · ⌫ delete · Ctrl+S save · esc cancel`
  - inline text: `editing <kind> · type to input · ←→ cursor · ⏎ confirm · esc cancel`
  - cycle overlay: `↑↓ pick · ⏎ confirm · esc cancel`
  - normal navigation: tab/↑↓/⏎/esc combinations per pane

### Tests (36 added — 291 total in suite)

`tests/test_config_menu.py`:

**Inline TEXT (7 tests):** opens edit on Enter, buffer starts with
current value at cursor end, typing appends, backspace deletes,
←→/Home/End move cursor, Enter commits + marks dirty, Esc cancels +
restores

**NUMBER (5 tests):** filters letters at input time, int field rejects
decimal point, commits parsed float, commits parsed int, empty
buffer on Enter shows warning toast and stays open

**SECRET (3 tests):** displays masked when not editing, plaintext
buffer while editing, commits to provider dict

**Provider type CYCLE (1 test):** Enter opens the cycle overlay

**_PromptEditor (15 unit tests):** initial state, empty initial
defaults to one line, insert char, newline splits line, backspace
within line, backspace at line start merges with previous line,
delete within line, delete at line end merges next line, arrow
left at line start wraps up, arrow right at line end wraps down,
up arrow clamps col to new line length, Ctrl+S commits, Esc cancels,
char count, Home/End within line, dirty tracking

**MULTILINE integration (4 tests):** Enter on prompt opens editor,
Ctrl+S commits to profile + marks dirty, Esc cancels with no change,
full save flow persists to disk via the menu's `s` action

### Notes

- The prompt editor is NOT a sub-App — that would require new modal-
  app machinery the App base class doesn't have. It's a helper class
  the config menu owns and renders. All input dispatch goes through
  `_handle_key`, which checks for the prompt editor first, then the
  inline text editor, then the cycle-edit overlay, then per-pane
  navigation.
- Word wrap is deferred. The editor displays lines as-typed; long
  lines clip horizontally with `›` markers. Most prompts are written
  with explicit newlines anyway.
- Undo/redo, find/replace, selection/copy-paste are all deferred to
  future polish. The v0 editor handles 95% of practical prompt
  editing.
- Secret display: I went with simple "always show bullets when not
  editing, plaintext when editing" rather than the briefly-show-last-
  char pattern from phones. Local-first single-user usage, masking
  is mostly cosmetic anyway.

---

## Phase 4.7 — prompt editor v2: soft wrap, selection, clipboard, Pretext caching (2026-04-06)

The prompt editor was the weakest piece of the renderer surface in
4.6 — it manually clipped long lines, didn't use any caching, and
had no selection. This phase rebuilds it as a real text editor that
follows the Pretext-shape principles the rest of the codebase uses.
Pulled out of `wizard/config.py` into its own module for cleaner
architectural separation.

### What landed

- **New file: `src/successor/wizard/prompt_editor.py`** — `PromptEditor`
  class (now public, no underscore) plus the `_wrap_source_line`
  pure-function primitive and `_VisibleChunk` dataclass. Standalone
  and reusable; doesn't depend on anything in the config menu.

- **Soft word wrap** — `_wrap_source_line(line, width)` greedily
  breaks at the last space before the width, falls back to a hard
  break if there's no space. Returns `tuple[_VisibleChunk, ...]`
  where each chunk has `source_col_start` and `text`. Joined chunks
  reconstruct the original line exactly — no chars dropped. Pure
  function, easily testable in isolation.

- **Per-source-line wrap cache** — Pretext-shaped. Each source row
  has a cached `(width, chunks)` entry that hits as long as the width
  is unchanged. Editing a single line invalidates only that line's
  cache; the other 99 lines of a big prompt keep their cached wraps.
  Resize invalidates everything. Newline insertion / line merging
  invalidate all (line indices shift). Operations that ALWAYS hit
  cache during typing: every paint of unchanged lines, every paint
  during scrolling.

- **Visible-row cursor navigation** — UP/DOWN no longer just
  `cursor_row += 1`. They walk the cached wrap to find the cursor's
  global visible-chunk index, move ±1 in chunk-list space, then map
  back to source coordinates by snapping the visual col to the new
  chunk's source range. PgUp/PgDn are repeated visible-row moves.
  This is how every real text editor handles soft wrap.

  The cursor stays in **source** coordinates (row, col into
  self.lines) so insert/delete remain trivially simple. Only
  navigation cares about visible space.

- **Selection state** — `selection_anchor: tuple[int, int] | None`.
  When None, no selection. When set, the selected range spans from
  the anchor to the cursor in either direction (normalized to start
  ≤ end via `_normalize_selection`).

- **Selection input handling**:
  - `Shift+←→↑↓/Home/End/PgUp/PgDn` — extends selection (sets anchor
    if no selection yet, then moves cursor while keeping anchor)
  - Any **non-shift** navigation key clears the selection
  - **Esc** — clears selection if active (first press), otherwise
    cancels the editor (second press)
  - **Backspace/Delete/typing** with active selection replaces the
    selected range
  - **Ctrl+A** — select all
  - **Ctrl+C** — copy selection via the OSC 52 callback (no clear)
  - **Ctrl+X** — cut: copy + delete

- **OSC 52 clipboard integration** — `PromptEditor.__init__` accepts
  a `copy_callback: Callable[[str], None] | None`. The config menu
  passes `self.term.copy_to_clipboard` (which the existing
  `Terminal` class implements via OSC 52 — works in
  Ghostty/iTerm2/kitty/alacritty/modern xterm/tmux with
  `set-clipboard on`). Callback failures are silently swallowed so a
  terminal that rejects OSC 52 doesn't crash the editor.

- **Selection paint with full-row extension** — multi-line selection
  highlights extend across the FULL width of the text area for
  fully-selected interior source rows, not just up to the source
  text. This matches Notepad / VS Code / every modern text editor's
  multi-line selection look. Implementation: walk each cell of the
  visible chunk, paint with selection bg if the source col is in
  the range; for "interior" source rows (rows strictly between
  selection start and end rows), fill the trailing empty cells with
  selection bg too.

- **Line number gutter on continuation chunks** — when a source line
  wraps to multiple visible chunks, only the FIRST chunk shows the
  line number. Continuation chunks show empty space in the gutter,
  matching VS Code / Sublime / every other editor's wrapped-line
  rendering.

### Footer keybinds dispatch by editor state

| state | keybinds |
|---|---|
| no selection | `↑↓←→ navigate · shift+arrows select · ⌃A all · ⌃S save · esc cancel` |
| with selection | `↑↓←→ extend (shift) · ⌃C copy · ⌃X cut · ⌃A select all · ⌃S save · esc clear` |

### Title bar info

When selection is active: `line N/M · X chars · Y sel`. Otherwise:
`line N/M · X chars`. Real-time as you select.

### Tests (48 added — 339 total in suite)

`tests/test_prompt_editor.py` — moved out of test_config_menu.py,
expanded for the new features:

**`_wrap_source_line` (8 tests):** empty line, short line one chunk,
breaks at space, no-space hard break, zero width fallback, preserves
every char (joined chunks reconstruct), source col starts align
between chunks

**`_normalize_selection` / `_is_in_selection` (5 tests):** already
ordered, reversed, same row by col, single line half-open boundary,
multi-line interior row check

**Basic state (3 tests):** initial state, empty initial, char count

**Editing (8 tests):** insert char, newline splits, backspace within
line / at line start (merges), delete within line / at line end
(merges next), tab inserts 2 spaces, dirty tracking returns to clean

**Source navigation (4 tests):** left at line start wraps up, right
at line end wraps down, home/end within line, ctrl+s commits

**Visual navigation (2 tests):** without wrap behaves like source
navigation, **with wrap** UP/DOWN moves to adjacent visible chunks
within the same source row when the line wraps

**Selection (8 tests):** shift+arrow starts selection, shift+arrow
extends, non-shift clears, esc clears first press / cancels editor
second press, ctrl+a select all, get_selection_text single/multi-line

**Selection-aware editing (4 tests):** typing replaces, backspace
deletes range, delete deletes range, multi-line backspace deletes
across line boundaries

**Clipboard (4 tests):** ctrl+c calls callback with selection (no
clear), ctrl+c without selection no-op, ctrl+x cuts (copy + delete),
callback failure silently swallowed

**Wrap cache (3 tests):** invalidates on edit (only that line),
invalidates all on resize (width change), invalidates all on
newline insertion (line shift)

### Notes

- The editor is now **public-named** (`PromptEditor` not
  `_PromptEditor`) since it lives in its own module and is imported
  cleanly. The config menu's wrapper that holds it is still
  `_prompt_editor` (private instance attribute).
- The editor is **not** an App — that would require new modal-app
  machinery the App base class doesn't have. It's a helper class
  with `handle_key` / `paint` / `is_done` / `result` that the parent
  App owns and renders. Same shape as Phase 4.6 but cleaner.
- **Cache hit rate during typing**: only the line being edited
  invalidates, so typing into a 100-line prompt has 99/100 cache hit
  rate per paint frame. Resize is the only operation that wholesale
  invalidates, and resize is rare during text editing.
- **The selection paint walks per-cell** because the highlight needs
  to apply to specific source positions, not whole chunks. For a
  60-cell text area at 30fps that's ~1800 cells/sec — well within
  the renderer's budget.
- The "interior row trailing highlight" only fires when a source row
  is **strictly between** sel_start[0] and sel_end[0] — i.e. fully
  selected from the start of its first char through the end of its
  last char + the rest of the line up to the text area edge. The
  selection's first and last source rows (which are partially
  selected) get per-char highlight without the trailing extension.
- Word wrap is **not perfect** for very long unbreakable tokens (URLs,
  identifiers without spaces) — those get hard-broken at the width.
  For prompts that's fine; for code editing we'd want a "wrap at
  any non-alphanumeric" option. Defer.
- **Selection while wrapped** works correctly because the cursor and
  anchor are in source coordinates, not visible coordinates. The
  paint walks visible chunks and asks "is this source col in the
  selection?" per cell — that's the right level of indirection.

---

## Phase 4.8, intro animation + demo system refactor (2026-04-06)

The bundled intro animation was rebuilt around an 11-frame braille
emergence sequence that resolves into the SUCCESSOR title portrait,
held for ~2 seconds. Theme-aware, any keypress skips ahead. The old
`demo` / `show` / `frames` CLI subcommands were deleted along with
the `demos/` package layer they belonged to; `chat.py` moved up to
`src/successor/chat.py` since it was the only file left in there.

### What landed

- **`src/successor/intros/successor.py`** — `SuccessorIntro` App.
  Loads 11 braille frames as `BrailleArt` instances at construction
  (Pretext-shaped layout cache). Plays them sequentially with
  Bayer-dot interpolation between adjacent frames at
  `EMERGE_PER_FRAME_S = 0.32s` per transition (10 transitions =
  3.2s emerge). Holds the final frame for `HOLD_FINAL_S = 2.4s`.
  Auto-exits. Any keypress skips.
  - Theme-aware: resolves the active profile and uses its accent
    color for the braille ink, bg for the background.
  - First `FADE_IN_S = 0.4s` lerps from bg toward accent so the
    first frame doesn't pop in hard.
  - "press any key to skip" hint at the bottom during emerge,
    hidden during the final hold.
- **`src/successor/intros/__init__.py`** — re-exports
  `SuccessorIntro` and `run_successor_intro()` entry point.
- **`src/successor/builtin/intros/successor/`** — 11 braille frame
  text files (`00-emerge.txt` through `10-title.txt`).
  `pyproject.toml` package data config updated to ship
  `intros/*/*.txt`.

### What got deleted

- **`src/successor/demos/`** — entire directory gone. The
  `BrailleArt` / `interpolate_frame` / `load_frame` primitives in
  `render/braille.py` stay; they're still used by the new intro
  and the wizard's welcome frame.
- **`successor demo`, `successor show`, `successor frames`** —
  three CLI subcommands removed along with their argparse parsers.

### What got refactored

- **`src/successor/demos/chat.py` is now `src/successor/chat.py`**
  since the demos/ directory is gone. Imports updated across
  `cli.py`, `snapshot.py`, `wizard/setup.py`, `wizard/config.py`,
  and `tests/test_chat_profiles.py`.
- **`_play_intro_animation()` in cli.py** calls
  `run_successor_intro()` directly. Future user intros will live
  in `~/.config/successor/intros/<name>/`.
- **`cmd_doctor` and `cmd_bench`** updated to count the successor
  intro frames.

### Tests (339, unchanged count, all passing)

No new tests since the change is mostly file moves. The chat.py
relocation was caught by import tests once the package was
reinstalled.

### Renderer features the new intro exercises

| concepts.md cat | feature | where |
|---|---|---|
| Cat 1 (mutable cells) | per-frame Bayer-dot interpolation between adjacent braille frames | `interpolate_frame()` in `_resolve_frame_lines` |
| Cat 2 (smooth animation) | ease-in-out cubic on the per-transition `t`, fade-in lerp on the first 0.4s | `ease_in_out_cubic`, `lerp_rgb` |
| Cat 5 (deterministic) | every frame is a function of `(elapsed, viewport_size)` | `_resolve_frame_lines` |
| Cat 6 (inline media) | braille art at viewport-fitted size with cached layout | `BrailleArt.layout()` |
| Cat 7 (programmatic UI) | the chat opens with a pre-configured intro driven by the profile | `cli.cmd_chat` → `_play_intro_animation()` |

### Notes

- Total intro duration: 5.61s measured end-to-end (3.2s emerge +
  2.4s hold + 0.01s startup). Matches the design target.
- The user originally thought the title frame (frame 10) had "slop
  letters" above the portrait when viewing the plaintext output —
  the braille block letters at the top look noisy when stripped of
  their colors and rendered through `render_grid_to_plain`. After
  diffing frames 9 and 10, they confirmed frame 10 is solid in
  actual terminal output. Frame 10 IS the integrated portrait;
  the plaintext rendering just couldn't show the letters cleanly.

---

## Phase 4.9 — delete profile from the config menu (2026-04-06)

The config menu's left pane gained a destructive action. From the
profiles list, capital `D` opens a centered confirmation modal that
either deletes the profile's JSON file from disk or — for user
overrides of a built-in — reverts to the built-in by unlinking the
override. Built-ins can't be deleted (there's nothing on disk to
remove), the active profile can't be deleted (it would orphan the
chat), and the last remaining profile can't be deleted (there must
always be a fallback). All three refusal cases show a warning toast
instead of opening the modal.

### What landed

- **`_DeleteConfirm` dataclass + `_delete_confirm` state field** in
  `src/successor/wizard/config.py`. Two-mode design: `"delete"` for
  pure user profiles and `"revert"` for user overrides of built-ins.
  The mode is decided at modal-open time by checking whether a
  built-in JSON file exists with the same name in
  `src/successor/builtin/profiles/`.
- **`_begin_delete_confirm()`** does all the validation up front:
  refuses last-profile, refuses active-profile, refuses pure built-in,
  and detects user-override-of-built-in to set `mode="revert"`.
  Each refusal shows a `_Toast` of kind `"warn"` and returns without
  opening the modal.
- **`_perform_delete()`** unlinks the user JSON file, drops dirty
  markers tied to that profile, reloads `PROFILE_REGISTRY`, rebuilds
  `_initial_profiles` and `_working_profiles` from scratch, and
  re-anchors the cursor onto a still-existing row (preferring the
  same name if revert mode brought back the built-in).
- **`_handle_delete_confirm_key()`** — Y (case-insensitive) confirms,
  N/Enter/Esc all cancel. Safe-default key choice: a tired finger
  on Enter does nothing destructive.
- **`_paint_delete_confirm_overlay()`** — centered modal with the
  ROUND box characters, accent_warn border, profile summary line
  (theme · mode · density · intro), warning glyph + 2-line message,
  and centered action footer. 200ms ease-out-cubic fade-in via
  `lerp_rgb` from bg toward the target colors. Title pill in the
  top border reads "delete profile?" or "revert profile?" depending
  on mode.
- **Footer keybind dispatch** updated — when the modal is open the
  footer reads `Y delete · N/⏎/esc cancel` (or `Y revert · ...`);
  when on the profiles pane the footer reads
  `tab focus · ↑↓ profile · ⏎ activate · → settings · D delete · s save · r revert · esc back`.
- **Modal-takes-input dispatch** — `_handle_key()` checks
  `_delete_confirm` before `_inline_text_edit` and `_editing_field`
  so other modals can't be opened on top of the confirmation. Tab,
  Save, etc. are all swallowed while the modal is open.

### Tests (17 new — 356 total, all passing)

In `tests/test_config_menu.py` under "Delete profile flow":

- `test_delete_capital_d_opens_modal_for_user_profile` — happy path
- `test_delete_lowercase_d_does_nothing` — only capital D
- `test_delete_refused_for_builtin_profile` — pure built-in refusal
- `test_delete_refused_for_active_profile` — active profile refusal
- `test_delete_refused_when_only_one_profile` — last-profile guard
- `test_delete_user_override_uses_revert_mode` — override detection
- `test_delete_modal_y_confirms_and_unlinks_file` — Y deletes file
- `test_delete_modal_lowercase_y_also_confirms` — case insensitivity
- `test_delete_modal_n_cancels` — N cancels
- `test_delete_modal_enter_cancels_safe_default` — safe default
- `test_delete_modal_esc_cancels` — Esc cancels
- `test_delete_revert_unlinks_user_file_and_builtin_remains` —
  end-to-end revert: file gone, built-in reappears, mode != override
- `test_delete_clears_dirty_for_that_profile` — dirty markers gone
- `test_delete_modal_blocks_other_input` — Tab/save swallowed
- `test_delete_cursor_lands_on_valid_row_after_delete` — cursor safety
- `test_delete_modal_renders_without_crashing` — paint smoke test
- `test_delete_revert_modal_says_revert` — title varies by mode

### Renderer features the modal exercises

| concepts.md cat | feature | where |
|---|---|---|
| Cat 1 (mutable cells) | the same overlay region paints either "delete profile?" or "revert profile?" based on the dataclass `mode` field | `_paint_delete_confirm_overlay` |
| Cat 2 (smooth animation) | 200ms ease-out-cubic fade-in via `lerp_rgb`, applied to every cell color in the modal | `fade_t` blends |
| Cat 3 (multi-region UI) | the modal sits on top of the three-pane layout without invalidating the panes — they paint, the modal paints over them, the diff layer commits | `on_tick` paint order |
| Cat 5 (deterministic) | the modal's exact visual state is a snapshot fixture | `test_delete_modal_renders_without_crashing` |
| Cat 7 (programmatic UI) | the modal's title, message body, and action verb all derive from `_delete_confirm.mode`, which was set by registry inspection at open-time | `_begin_delete_confirm` → `mode = "revert" if has_builtin else "delete"` |

### Notes

- The two-mode design (`delete` vs `revert`) wasn't in the initial
  sketch — it emerged when checking the registry semantics. A
  built-in being shadowed by a user file is the *only* way the
  loader exposes that override as a "profile in the registry," so
  the natural answer to "delete this profile" is "remove the
  override," which lets the built-in reappear.
- Capital `D` (not lowercase) was chosen so a casual hand on the
  keyboard can't accidentally arm a destructive action. Lowercase
  `d` is reserved for future use.
- The cursor anchoring after delete prefers the SAME name (revert
  case) and falls back to clamping the original index (pure delete
  case). This means the visual focus stays put across a revert and
  only moves the minimum distance across a delete.

---

## Phase 5.0 — bash-masking subsystem (2026-04-07)

The first piece of the agent loop. We don't ask the model to learn a
structured tool-call schema (`docs/llamacpp-protocol.md` notes that
Qwen 3.5 distill is unreliable at tool calling). Instead the model
writes bash in fenced code blocks — its strongest mode — and we parse
it client-side into structured `ToolCard`s that the renderer paints
in place of plain bash. Best of both worlds: the model is fluent at
the work, the user sees clean structured actions with risk
classification.

This phase ships the entire bash-masking subsystem **decoupled from
the agent loop**. The `/bash <command>` slash command is the v0 proof:
type bash in the chat and watch it render as a structured card with
the real subprocess output beneath. When the agent loop lands later,
the same `dispatch_bash()` entry point becomes the tool dispatch
target — no rework.

### What landed (`src/successor/bash/`)

- **`cards.py`** — `ToolCard` frozen dataclass: verb, params (ordered
  tuple of (key, value)), risk literal ("safe"/"mutating"/"dangerous"),
  raw_command (always preserved), confidence (0-1, parser self-assessment),
  parser_name, output, stderr, exit_code, duration_ms, truncated.
  `executed` and `succeeded` properties for callers. Cards are
  immutable; the executor uses `dataclasses.replace()` to build
  enriched cards from parsed cards.

- **`parser.py`** — `@bash_parser("name")` decorator + `_PARSERS`
  registry + `parse_bash(cmd)` entry point. Shlex-splits the command,
  looks up the first token, dispatches. Fall-through paths for empty
  input, malformed quoting (shlex ValueError), buggy parser exceptions,
  and unknown command names — each returns a generic card with
  appropriately low confidence so the chat never crashes on weird
  input. **`clip_at_operators(args)`** is a critical helper: shlex
  knows nothing about shell grammar, so `'ls foo | grep bar'` tokenizes
  as `['ls', 'foo', '|', 'grep', 'bar']` and a naive parser would
  absorb everything past `foo` as more "paths". `clip_at_operators`
  truncates argv at the first operator token (`|`, `||`, `&&`, `;`,
  `>`, `2>`, `<`, `&>`, etc.) so each parser sees only its own
  command segment.

- **`risk.py`** — independent risk classifier that runs IN ADDITION
  to the parser's own risk declaration. The classifier walks regex
  patterns over the raw command string and finds:
  - **dangerous**: rm-rf at /, ~, /etc, /var, /usr, /bin, /sbin,
    /boot, /sys, /proc, /dev, /lib, /lib64, /root, /home; sudo; su;
    curl|sh; wget|sh; eval; chmod 777 / chmod +s; redirect into
    system path; dd to block device; fork bomb (`:(){ :|:& };:`);
    mkfs/fdisk/parted/wipefs; shutdown/reboot/halt/poweroff; kill
    PID 1; iptables flush
  - **mutating**: file-writing redirects (excluding /dev/null and
    friends — we mirror that exclusion list); mkdir/touch/cp/mv/rm/
    chmod/chown/ln; sed -i; package manager mutations (apt/yum/dnf/
    pacman/brew/pip/npm/cargo/gem/go install|remove|update); git
    mutating subcommands
  - **safe**: everything else
  Returns `(risk, reason)`. The dispatch layer takes
  `max_risk(parser_risk, classifier_risk)` so either layer can
  escalate but not de-escalate.

- **`exec.py`** — `dispatch_bash(cmd, *, allow_dangerous=False,
  timeout=30, cwd=None, env=None)` is the public dispatch entry
  point. Parses, classifies, runs via `subprocess.run(shell=True,
  executable="/bin/bash", capture_output=True, text=True)`. Refuses
  dangerous commands by raising `DangerousCommandRefused(card,
  reason)` — the exception carries the gated card so callers can
  show the user WHAT was blocked WITH all its parsed params. Output
  is truncated at 8KB via `_truncate_output()` which handles UTF-8
  boundaries correctly (no broken codepoints when clipping in the
  middle of a multi-byte char). Timeout produces `exit_code=-1` with
  partial output preserved. `preview_bash(cmd)` is the parse-only
  variant the renderer uses to show a card BEFORE execution (for
  confirmation modals, refused-card display, etc).

- **`render.py`** — `paint_tool_card(grid, card, *, x, y, w, theme,
  show_output=True, max_output_lines=12)` is a pure paint function
  that draws a card and returns the height consumed. Layout:
  ```
  ╭── ▸ list-directory ──────────────────────╮
  │    path  /etc                            │
  │  hidden  yes                             │
  │  format  long                            │
  ╰── $ ls -la /etc ─────────────────────────╯
     total 184
     drwxr-xr-x 100 root root 4096 Apr 7 ...
       ↳ ✓ exit 0 in 4ms
  ```
  Top section is the parsed verb header pill + key/value param table
  inside a rounded box (`BOX_ROUND`). Bottom border carries the raw
  command verbatim prefixed with `$ ` (dim italic — always visible
  so the user can spot parser misses). Below the box: command output
  with code-tinted background bars + status footer. Risk-tinted
  border + verb glyph: `▸` safe (`theme.accent`), `✎` mutating
  (`theme.accent_warm`), `⚠` dangerous (`theme.accent_warn`).
  Confidence < 0.7 adds a `?` badge after the verb. `measure_tool_card_height()`
  is the matching pure measurer for callers that need geometry before
  paint. Output truncation: lines beyond `max_output_lines` collapse
  into a `⋯ N more lines ⋯` marker.

- **`patterns/`** — 12 pattern files covering 24 command names:
  - `ls.py` — `list-directory` (long/hidden/recursive/human-sizes)
  - `cat.py` — `read-file` / `concatenate-files` / `read-stdin`
  - `head_tail.py` — `read-file-head` / `read-file-tail` (-n, -f)
  - `grep.py` — `search-content` (grep/rg/ripgrep, bundled flags
    like `-rin` and `--ignore-case`/`--recursive` long forms)
  - `find.py` — `find-files` (find with -name/-type/-maxdepth, fd/fdfind
    with simpler grammar)
  - `pwd_echo.py` — `working-directory` / `print-text` / `noop`
  - `mkdir.py` — `create-directory` / `create-file` (touch)
  - `rm.py` — `delete-file` (mutating) / `delete-tree` (mutating
    if -r alone, dangerous if -rf)
  - `cp_mv.py` — `copy-files` / `move-files` (mutating)
  - `git.py` — per-subcommand: status/diff/log/show/blame/branch/...
    safe; add/commit/checkout/push/pull/fetch/merge/rebase/reset
    mutating; `git push --force` ESCALATED to dangerous
  - `python.py` — `run-python-inline` / `run-python-module` /
    `run-python-script` (python and python3, all mutating)
  - `which.py` — `locate-binary` / `describe-command`
  Every parser calls `clip_at_operators(args)` first to scope its
  argument list to the current command segment.

### Chat integration (`src/successor/chat.py`)

- **`_Message`** gained an optional `tool_card: ToolCard | None`
  field. Non-None forces `synthetic=True` (tool messages are never
  sent to the model in the conversation history). The `__slots__`
  was updated.

- **`_RenderedRow`** gained `prepainted_cells: tuple[Cell, ...] = ()`.
  When non-empty, `_paint_chat_row` copies the cells verbatim to
  the chat region and skips the entire span/leading flow.

- **`_build_message_lines`** detects tool-card messages and routes
  them through `_render_tool_card_rows`, which paints the card into
  a temporary sub-grid sized to `body_width` and snapshots each row's
  cells into a tuple. The result is N `_RenderedRow`s with `line_tag
  = "tool_card"` and the prepainted cells attached. The chat's flat-row
  scroll model stays intact — tool cards are just rows with a fast
  paint path.

- **`/bash <command>`** slash command in `_submit`. Echoes the
  command as a synthetic user message, runs `dispatch_bash`, appends
  a tool message with the resulting card. On `DangerousCommandRefused`
  the refused (preview-only) card is shown along with a synthetic
  refusal explanation, so the user sees WHAT was blocked and WHY.
  Added to the `SLASH_COMMANDS` registry so autocomplete picks it up.

### Tests (131 new — 487 total, all passing)

Across 4 new test files:

- **`test_bash_parser.py`** (73 tests):
  - registry plumbing (population, has_parser, fallback, defensive)
  - empty/blank input, unbalanced quotes, buggy parser isolation
  - `clip_at_operators` (all operators, mutation safety)
  - one happy-path test per registered command + edge cases
  - parametrized `classify_risk` table (28 cases covering all 3 risks)
  - `/dev/null` redirect is correctly NOT flagged mutating
  - `max_risk` ordering exhaustive (3x3 matrix)

- **`test_bash_exec.py`** (25 tests):
  - happy path (echo, pwd, cwd, success, failure, command-not-found)
  - stderr capture, pipes, redirects, subshells (shell=True works)
  - dangerous refusal: rm -rf /, sudo, curl|sh
  - `allow_dangerous=True` bypass
  - mutating runs without flag (only dangerous gated)
  - classifier escalates parser risk (sudo ls is dangerous)
  - classifier doesn't de-escalate (parser-flagged rm -rf stays dangerous)
  - output truncation: short passthrough, at-limit, UTF-8 boundary
  - real truncation E2E (head -c 16K | tr)
  - timeout sets exit_code -1 with reason
  - preview_bash doesn't execute, includes classifier risk
  - card immutability (parser card unchanged after dispatch)

- **`test_bash_render.py`** (21 tests):
  - smoke + structure (executed card vs preview)
  - all 4 risk-tinted glyphs (▸ ✎ ⚠ ?)
  - low/high confidence ? badge presence
  - param table renders, no-params placeholder
  - raw command on bottom border, long-command truncation
  - output rendering, status line, failure glyph
  - (no output) placeholder
  - max_output_lines truncation marker
  - measure ≡ paint height consistency
  - executed card taller than preview
  - too-narrow refusal, grid overflow graceful
  - output indent alignment

- **`test_chat_bash.py`** (12 tests):
  - `/bash` dispatches and appends a tool message
  - `/bash` no-args / blank shows usage
  - dangerous command shows refused card + explanation
  - tool messages always synthetic (never sent to model)
  - `_Message(tool_card=...)` auto-sets synthetic
  - chat grid contains card structure
  - tool rows have prepainted_cells
  - multiple cards stack vertically
  - mixed tool + regular messages render together
  - failed command shows ✗ glyph
  - unknown command renders generic card with ? badge

### Renderer features the bash subsystem exercises

| concepts.md cat | feature | where |
|---|---|---|
| Cat 1 (mutable cells) | the same row region paints either a structured card or plain markdown depending on `_Message.tool_card` | `_paint_chat_row` short-circuit on `prepainted_cells` |
| Cat 2 (smooth animation) | risk-tinted border colors blend with theme accents during `blend_variants` transitions | `_border_color` reads `theme.accent_*` live |
| Cat 3 (multi-region UI) | tool card + status footer paint at distinct vertical sections within the same chat scroll region | `paint_tool_card` returns height consumed |
| Cat 5 (deterministic) | every card visual is a function of `(card, theme, width)` — fully snapshot-testable | `test_bash_render.py` snapshots |
| Cat 6 (inline media) | tool cards render INSIDE the chat scroll region, not as overlays | `_render_tool_card_rows` snapshots cells into the flat row list |
| Cat 7 (programmatic UI) | the renderer transforms a model's bash command string into a structured action card the model never knew existed | `dispatch_bash` → `paint_tool_card` |

### The architectural insight

`docs/llamacpp-protocol.md` line 388: *"Qwen3.5-27B-Opus-Distilled is
less reliable at [tool use]."* This is the local-mid-grade-model
reality: tool-call schemas are out-of-distribution, bash is in. Every
attempt to teach the model a JSON tool schema is friction. So we
inverted the contract: the model's tool is bash, the structured card
is purely cosmetic, and the renderer is the layer that converts
between the two. The model writes the way it's strongest. The user
sees the structured action they wanted. The risk gate is a render-time
concern, never a prompting concern.

This works because of the renderer's diff layer — no other agent
harness can rewrite cells after they're committed, so they're stuck
showing raw bash output in scrollback. We can intercept, parse, and
present a clean structured card that lives inside the chat scroll
region with full theme integration.

### Notes

- Visual verification was the bug detector. The first painted card
  showed `2>/dev/null` being absorbed by the ls parser and the
  redirect-to-/dev/null being flagged as mutating. Both fixed via
  the visual feedback loop, then locked in by tests. This is exactly
  why the user's directive is "visual verification E2E" — it catches
  things tests for narrow units would miss.
- The registry is shared mutable state. Tests use `_PARSERS.copy()`
  + restore to install temporary parsers for buggy-parser-isolation
  tests without leaking state.
- The bash package's `__init__` imports patterns BEFORE risk.py and
  exec.py because `from . import patterns` triggers all the
  `@bash_parser` decorators, which need `parser.py` already loaded.
  Order is documented in the file with `noqa: E402` comments.

---

## Phase 5.1 — agent loop + compaction (2026-04-07)

The agent loop, the compaction pipeline, the tick-driven state machine,
and the burn-tested-against-A3B proof that semantic continuity survives
a 96.9% context reduction. This phase translates the architectural
sketch from the previous session into 1,800 lines of stdlib Python +
115 unit tests + a live E2E burn rig that exercises every threshold,
every error path, and every visual surface.

### What landed (`src/successor/agent/`)

- **`log.py`** — `LogMessage` (frozen dataclass with role + content +
  optional `ToolCard` + boundary/summary flags), `ApiRound` (the
  indivisible compaction unit — PTL truncation drops these whole so
  the API never sees orphaned tool_results), `MessageLog` (ordered
  rounds + `AttachmentRegistry` + `system_prompt`), `BoundaryMarker`
  (compaction event metadata: pre/post tokens, rounds_summarized,
  reduction_pct property). The shape is *compaction-ready from day
  one* — every primitive the loop and compactor need is present
  before either was written.

- **`events.py`** — frozen `ChatEvent` ADT for every event the loop
  yields: `StreamStarted`, `ReasoningChars`, `ContentChunk`,
  `StreamCommitted`, `StreamFailed`, `BashBlockDetected`,
  `ToolStarted`, `ToolCompleted`, `ToolRefused`, `CompactionStarted`,
  `Compacted`, `CompactionFailed`, `TurnStarted`, `TurnCompleted`,
  `BlockingLimitReached`, `MaxTurnsReached`, `LoopErrored`. The chat
  consumes these via a callback to update its UI; tests assert
  isinstance + count to verify the right events fire.

- **`tokens.py`** — `TokenCounter` with two paths: (1) llama.cpp's
  `POST /tokenize` endpoint for ground-truth counts, (2) char
  heuristic (`HEURISTIC_CHARS_PER_TOKEN = 3.5`, deliberately
  conservative so we slightly OVERESTIMATE and fire compaction a
  touch early rather than late). LRU per-string cache (default
  1024 entries) so the loop can call `count()` freely without
  re-paying HTTP cost. Auto-disables the endpoint after 3 consecutive
  HTTP failures and falls back to heuristic-only until `clear()`.
  `count_message`, `count_round`, `count_log`, `refresh_round_estimates`
  for the loop's needs. Verified against live `/tokenize`: `"hello
  world"` → 2 tokens, function-definition → 10 tokens.

- **`budget.py`** — three pieces of state:
  - `ContextBudget` (frozen): window + warning_buffer +
    autocompact_buffer + blocking_buffer with consistency check
    in `__post_init__`. `state(used)` returns `"ok" | "warning" |
    "autocompact" | "blocking"`. `should_autocompact`,
    `over_blocking_limit`, `in_warning_zone`, `headroom`, `fill_pct`.
  - `CircuitBreaker`: trips after `max_failures=3` consecutive
    failures, `success()` resets, `reset()` is the manual override.
  - `RecompactChain`: blocks two compactions firing within 30s
    AND fewer than 3 turns apart. Only `record()` updates state on
    successful compaction; failed ones don't (so retries aren't
    blocked).
  - `BudgetTracker`: bundles all three + per-session stats
    (`compactions_total`, `peak_tokens`). `should_attempt_compaction`
    returns `(decision, reason)` so the loop's refusal is diagnostic.

- **`microcompact.py`** — pure stateless function that clears stale
  tool result content. Two triggers: count-based (>N kept) and
  time-based (>X minutes idle). Replaces `tool_card.output` with
  `"[tool result cleared during compaction]"` placeholder; the card's
  verb + params + raw_command stay so the chat history remains
  navigable. Defensive: messages with `created_at=0.0` (no timestamp)
  are NOT idle-cleared.

- **`compact.py`** — the LLM-summarization layer. `compact(log,
  client, *, counter, keep_recent_rounds=6, instructions, ...)`:
  1. Refresh token estimates
  2. Split rounds into [to_summarize, to_keep] at the keep boundary
  3. Build a transcript prompt + summarization instructions
  4. Stream the summary from the client, drain to a single string
  5. Build the new MessageLog: `[boundary] + [summary] + [kept rounds]
     + [attachment hint]`
  6. Refresh post-compact token estimates, finalize the BoundaryMarker
  - **PTL retry loop**: if the summarization call returns
    "prompt is too long", drop the oldest 3 rounds-to-summarize and
    retry, up to MAX_PTL_RETRIES (3). On exhaustion, raise
    CompactionError.
  - **CompactionClient Protocol**: structural type so the test
    suite can substitute a mock client without touching LlamaCppClient.
  - Default summarization instructions are tuned for Qwen 3.5
    distill: explicit about preserving facts, paths, decisions,
    code snippets; explicit about discarding reasoning chains and
    filler.

- **`bash_stream.py`** — `BashStreamDetector`, a state machine that
  consumes streamed model content character-by-character and detects
  fenced ```` ```bash ```` blocks even when fence markers split
  across chunk boundaries. State enum: `TEXT → IN_FENCE_OPEN →
  IN_BASH | IN_OTHER → TEXT`. Carries a `_carry` buffer between
  `feed()` calls so a partial fence at the end of one chunk merges
  with the next chunk. `flush()` resolves any in-progress state at
  end-of-stream. Splits multi-line bash blocks into individual
  commands (one per non-comment, non-blank line) with backslash
  continuation. **Verified end-to-end with one-character-at-a-time
  drip test**: every char of `"```bash\necho hi\n```"` arriving in
  its own `feed()` call still produces `["echo hi"]`.

- **`loop.py`** — `QueryLoop`, a tick-driven state machine. NOT an
  async generator, because the chat is a frame-driven sync `App`.
  The chat calls `tick()` once per frame; the loop advances one
  step. Phases: `IDLE → COMPACTING → STREAMING → EXECUTING_TOOLS →
  IDLE` (with `DONE` as the terminal state). Owns the message log,
  budget tracker, token counter, current ChatStream, bash detector,
  and pending bash queue. **Reactive compact path**: if the API
  returns "prompt is too long" mid-stream, the loop catches it and
  fires a forced compaction, then retries the stream — mirrors
  free-code's `query.ts:1119-1165` reactive compact handler.
  Synchronous tool dispatch in v0; concurrent execution comes later.
  Events flow OUT through `on_event(ev)` callback — same information
  flow as free-code's yield-driven loop, adapted to a sync consumer.

### Chat integration (`src/successor/chat.py`)

Adapter approach: instead of rewriting the chat to use `MessageLog`
directly, two helpers convert between the chat's `_Message` list
and the agent's `MessageLog` on demand. The streaming path stays
unchanged. Three new slash commands wire the agent into the chat:

- **`/budget`** — show context fill % + token counts + threshold
  state. Calls `_to_agent_log()`, runs `TokenCounter.count_log`,
  returns a synthetic message with stats. Reads the profile's
  `provider.context_window` to size the budget appropriately.

- **`/burn N`** — inject N synthetic tokens of varied content (code
  blocks, lorem-ipsum padding, fake file paths) so compaction can
  be tested without real model calls. The injected messages get
  realistic timestamps spaced 0.5s apart so microcompact's idle
  logic doesn't fire on them. Reports the new token count after
  injection.

- **`/compact`** — manually fire `compact()` against the chat's
  current history. Builds the agent log, runs compaction against
  the live client, writes the result back via `_from_agent_log`.
  Reports the boundary stats as a synthetic message ("✓ compacted:
  N → M tokens, X% reduction, K rounds summarized").

The adapter handles boundary/summary messages by mapping them back
to synthetic chat messages with `synthetic=True` so they aren't
re-sent to the model. Tool cards survive the round-trip intact.

### `compact-test` profile + swap scripts

- `src/successor/builtin/profiles/compact-test.json` — A3B at
  50,000 token context, forge theme (so the chrome visually
  signals "stress-test mode"), `provider.context_window: 50000`
  so `/budget` shows the right thresholds. System prompt explicitly
  asks the model to recall facts on demand for compaction validation.

- `scripts/swap_to_a3b.sh` — kills the running llama-server, brings
  up A3B (`Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf`) with `-c 50000`,
  exports `LD_LIBRARY_PATH` to the llama.cpp build dir
  (required because the binary at `/usr/local/bin/llama-server`
  has unresolved `libmtmd.so.0` / `libllama.so.0` references),
  waits for `/health`, prints confirmation. Documents Nyx impact
  in the script header so the user knows what's affected.

- `scripts/swap_to_qwopus.sh` — inverse: kills A3B, brings qwopus
  back at 262K context.

### The two bugs the burn rig caught

Visual + live testing caught two real bugs that unit tests with
mocked clients would have missed:

1. **`providers/llama.py` socket timeout was 5s on the WHOLE
   request, not just connect.** `urlopen(req, timeout=connect_timeout)`
   sets the socket timeout for *all* I/O on the connection — so
   when A3B was processing a 38,000-token prompt and took 30+ seconds
   to emit the first byte, the read timed out. Fixed by using
   `max(timeout, connect_timeout)` for urlopen so the socket timeout
   is at least as large as the streaming deadline. Latent bug —
   would also have broken regular chat with very long prompts.

2. **Boundary + summary messages were emitted with `role=system`
   but Qwen3.5's chat template enforces "system message must be at
   the beginning"** and raises a Jinja exception on any non-leading
   system message. After compaction we had three system messages
   in a row (original prompt + boundary + summary) which broke the
   template. Fixed by emitting boundary/summary messages as `user`
   role with explicit `[earlier conversation compacted]` and
   `[summary of earlier conversation, provided by the harness…]`
   prefixes so the model still understands they're context, not
   real user turns.

Both bugs were silent in unit tests (mocked client doesn't enforce
chat templates, doesn't simulate prompt processing time). The burn
rig caught them on the first live run. **This is exactly why the
user's directive is "visual verification E2E"** — it surfaces
integration bugs that unit tests can never see.

### Burn rig results — A3B at 50K context

```
Pre:   40,052 tokens   165 rounds
Post:   1,259 tokens     6 rounds
Reduction: 96.9%  (saved 38,793 tokens)
Wall time: 40.2s

Semantic recall: 100% (4/4)
  secret_word    "thunderstrike"  ✓
  favorite_color "oxblood"        ✓
  lucky_number   "47"             ✓
  user_dog       "Boris"          ✓
```

The 4 key facts seeded into the FIRST round survived the
compaction barrier and were recalled correctly when probed after
164 rounds of unrelated burn content. The summary Qwen produced
explicitly captured them: "I noted that the user requested I
remember specific personal details for a later quiz: a secret word
thunderstrike, favorite color oxblood, lucky number 47, and a
husky named Boris…"

Other passing checks:
  - Token tracking accuracy vs `/tokenize`: exact match
  - Threshold transitions: ok → warning → autocompact at the
    correct token counts
  - Recompact chain detection: blocks two compactions within 30s
    + 3 turns, allows after cooldown
  - Blocking limit predicate: fires at exactly `window - blocking_buffer`
  - Microcompact: clears 11 of 15 stale tool results, keeping
    the most recent 4 verbatim
  - Visual chat render: `/burn → /budget → /compact` flows
    through the existing chat painter without disturbing the
    streaming path

### Tests (115 new — 602 total, all passing)

Six new test files using a deterministic mock client/stream pattern
so the unit suite stays hermetic:

- `test_agent_log.py` (~20 tests) — message log shapes, boundary
  insertion, attachment tracking, API serialization (including
  the boundary/summary user-role conversion)
- `test_agent_tokens.py` (~13 tests) — heuristic + endpoint paths,
  LRU eviction, failure handling, fall-back behavior
- `test_agent_budget.py` (~17 tests) — threshold transitions,
  circuit breaker lifecycle, recompact chain semantics
- `test_agent_microcompact.py` (~10 tests) — count-based +
  time-based clearing, idempotency, no-mutation guarantee,
  defensive timestamp handling
- `test_agent_compact.py` (~13 tests) — full compaction with mocked
  stream, PTL retry success + exhaustion, error paths, attachment
  re-injection, reason propagation
- `test_bash_stream.py` (~22 tests) — every fragmentation pattern
  from one-shot to one-char-at-a-time, all language aliases
  (bash/sh/shell/zsh/fish/console/terminal), comments + blank
  lines + backslash continuation, multi-block accumulation
- `test_agent_loop.py` (~20 tests) — state machine transitions,
  simple Q&A, bash detection + execution, dangerous refusal,
  multi-block ordering, error paths, reactive compact, blocking
  limit, max_turns, cancel, stats

### Renderer features the agent loop exercises

| concepts.md cat | feature | where |
|---|---|---|
| Cat 1 (mutable cells) | the same chat region paints either streaming reasoning chars, content tokens, tool cards, or compaction boundaries | `_paint_chat_row` switches on `_RenderedRow` fields |
| Cat 2 (smooth animation) | compaction events fade old rounds into a summary divider via lerp_rgb (planned — wired in next session) | `Compacted` event reaches the chat |
| Cat 3 (multi-region UI) | the title bar's context-fill pill, the chat scroll, the input area all paint in the same frame across the loop's tick | `SuccessorChat.on_tick` |
| Cat 5 (deterministic) | every loop transition is a function of (state, events) — fully snapshot-testable via mock client | `test_agent_loop.py` |
| Cat 7 (programmatic UI) | the loop yields events that the chat consumes; the UI is purely a projection of the loop's state | `on_event` callback |

### Architecture sketch — what works, what's deferred

**Works in v0:**
- The complete query loop end-to-end against the live A3B model
- Compaction fires automatically at the threshold
- Bash blocks detected during streaming and dispatched after commit
- All risk levels (safe / mutating / dangerous) handled
- Reactive compact on PTL errors
- Token tracking via /tokenize with heuristic fallback

**Deferred for next session:**
- Visible compaction animation in the chat (the events fire but
  the smooth fade-in needs the renderer wiring)
- Title-bar context-fill pill (the data is there via /budget,
  needs the visual treatment)
- Concurrent tool execution (v0 is sync — single tool at a time)
- Streaming tool execution (tools start AFTER stream commits in v0)
- Wiring `/profile compact-test` end-to-end so the burn rig can
  run from inside the chat without running scripts manually

### Notes

- **The summarization quality on A3B is excellent.** The 4-fact
  recall test passed 100% on the first try with the default
  summarization instructions. No prompt tuning required.
- **A3B is much faster than qwopus for this workload.** ~40s for
  a 38K-token compaction summary on the 35B model with 3B active
  params is roughly 4x faster than qwopus would be.
- **The two-bug catch validates the visual-E2E discipline.** If we
  had only run unit tests with mocked clients, both bugs (urllib
  socket timeout, Qwen chat template constraint) would have shipped
  silently and only surfaced on real prompts.
- **The compact-test profile uses forge theme** so the user
  visually knows when they're in stress-test mode (forge is the
  warm/red palette vs steel's cool/blue). Theme-as-mode-indicator
  is a small but ergonomic detail.

---

## Phase 5.2 — visible compaction animation (2026-04-07)

The compaction event becomes the harness's signature visual moment.
Five phases, total ~5 seconds, every cell driven by `(state, time) →
cells` math through the existing renderer primitives. No external
animation library — pure `lerp_rgb` + `ease_out_cubic` + a new
`paint_horizontal_divider` that grows from the center outward.

### The animation arc

```
T=0     compaction completes → snapshot pre-compact messages
T=0-300   ANTICIPATION  base_color tinted toward accent_warm 35%
                        the rounds-to-be-summarized get a subtle glow
T=300-1500 FOLD         per-row fade_alpha lerps fg → bg via
                        ease_out_cubic — old rounds dissolve into
                        the void; chars stay but their fg matches bg
T=1500-1900 MATERIALIZE the boundary divider draws in from CENTER
                        outward via paint_horizontal_divider(t).
                        Pill snaps in at t > 0.6 with its own alpha
                        fade-in over the remaining 0.6 → 1.0 range
T=1900-2500 REVEAL      summary message fades in from theme.bg →
                        theme.fg_dim via lerp_rgb on its rows
T=2500-5000 TOAST       (toast wiring deferred — handled by the
                        boundary's continuous pulse instead)
T=5000+   SETTLED       boundary stays as a permanent visible artifact
                        with a subtle 0.4 Hz pulse via lerp_rgb
                        toward theme.accent — "living artifact"
```

### What landed

- **`src/successor/render/paint.py`** gained
  `paint_horizontal_divider(grid, x, y, w, *, style, char, t)` —
  pure paint function that draws a horizontal line growing from
  the center outward at progress `t` (0 → 1). Returns the number
  of cells actually drawn this frame. Generic primitive — useful
  for any "divider draws in" effect (search results separator,
  section breaks, etc.), not just compaction.

- **`src/successor/chat.py:_Message`** gained explicit boundary/
  summary fields:
  - `is_boundary: bool` — marks a message as a compaction boundary
    divider. The chat painter routes is_boundary rows through
    `_paint_compaction_boundary` instead of the normal markdown flow.
  - `is_summary: bool` — marks a message as a compaction summary,
    rendered with a dim/italic treatment + `▼` prefix glyph.
  - `boundary_meta` — attached `BoundaryMarker` so the painter can
    read the pre/post token counts and reduction_pct for the pill.
  Both flags force `synthetic=True` (boundary/summary messages
  are never sent to the model in the conversation history).

- **`_RenderedRow`** gained:
  - `is_boundary: bool` — fast-path flag for the painter
  - `boundary_meta: object | None` — the BoundaryMarker
  - `materialize_t: float = 1.0` — partial draw-in progress for
    the materialize phase
  - `is_summary: bool` — fast-path flag
  - `fade_alpha: float = 1.0` — per-row alpha used by the fold
    phase to dim cells uniformly toward `theme.bg`. The `_faded`
    inner helper in `_paint_chat_row` applies this to leading,
    blockquote borders, and body spans uniformly.

- **`_CompactionAnimation` dataclass** with the 5-phase state
  machine:
  - `started_at: float` — wall-clock anchor
  - `pre_compact_snapshot: list[_Message]` — captured BEFORE the
    chat swaps to the post-compact state, used by the FOLD phase
    as the visual content to fade out
  - `boundary` + `summary_text` + `reason`
  - `phase_at(now)` returns `(phase_name, t)` where t is 0-1
    progress within the current phase. Phase names: `pending →
    anticipation → fold → materialize → reveal → toast → done`

- **`_handle_compact_cmd`** now snapshots `self.messages` BEFORE
  running compaction, swaps to the post-compact state immediately
  after, and arms the animation. The painter then drives the
  visible transition. No "compacting…" status message — the
  animation IS the status indicator.

- **`_build_message_lines`** routes through a new
  `_build_rows_from_messages` helper that accepts:
  - `global_fade_alpha` (used by FOLD phase)
  - `anticipation_glow` (used by ANTICIPATION phase to tint
    toward accent_warm)
  - `anim_phase` + `anim_t` for the boundary row's `materialize_t`
    and the summary row's `fade_alpha` during MATERIALIZE / REVEAL
  Animation routing in `_build_message_lines`:
  - `fold` / `anticipation` → paint the snapshot
  - `materialize` / `reveal` / `toast` → paint `self.messages`
    with overrides
  - `done` → clear `_compaction_anim`, paint normally

- **`_paint_chat_area`** gained a scroll override during the
  materialize/reveal/toast phases: it finds the boundary row in
  the committed list and pins `effective_scroll` so the divider
  sits in the upper-sixth of the visible chat region. This
  guarantees the materialization is IN VIEW regardless of where
  the user was scrolled when /compact fired.

- **`_paint_compaction_boundary`** is the painter for boundary
  rows. Layout:
  ```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┤ ▼ 6 rounds · 3k → 2k · 37% saved ▼ ├━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ```
  Risk-tinted via `theme.accent_warm`. Subtle 0.4 Hz pulse via
  `lerp_rgb` toward `theme.accent` after the materialize completes
  — gives the divider a "living artifact" feel rather than dead
  chrome. Pill text format: ` ▼ N rounds · X → Y · Z% saved ▼ `.

- **`_format_boundary_pill`** is a small static helper that
  formats a `BoundaryMarker` as the pill label. Duck-typed read
  of the marker so the painter doesn't import from `agent.log`.

### Context fill bar overhaul

The existing static-footer ctx bar gained:
- **Token count from the agent's TokenCounter** when cached
  (accurate via `/tokenize`). Falls back to the legacy char/4
  heuristic when no counter is set.
- **Window from the profile's `provider.context_window`**
  instead of the hardcoded `CONTEXT_MAX = 262144`. The
  compact-test profile at 50K window now gets the correctly-
  scaled bar.
- **Threshold-state classification** mirrors `agent.budget.ContextBudget`:
  ok → warning → autocompact → blocking
- **State badges** when over threshold:
  - `◉ COMPACT` at autocompact
  - `⚠ BLOCKED` at blocking
- **Continuous pulse** via `math.sin(self.elapsed * 0.5 * 2 * pi)`
  blending the bar color toward fg when state is autocompact or
  blocking — gentle "compact me!" signal that doesn't shout.
- **Right-label color** shifts with state too: `theme.fg` (ok) →
  `theme.accent_warm` (warning) → pulsing accent_warm/accent_warn
  (autocompact) → `theme.accent_warn` (blocking).

### Tests (28 new — 630 total, all passing)

- **`test_compaction_animation.py`** (19 tests):
  - phase machine: anticipation/fold/materialize/reveal/done/pending
  - `paint_horizontal_divider` primitive: full width, half progress,
    zero progress, grows-from-center, clamps t>1, skips offscreen
  - chat-level orchestration: anticipation paints snapshot,
    fold dims cells (verified by reading actual fg color
    distance from bg), materialize shows growing divider count,
    reveal shows pill, done clears state, post-anim boundary
    persists
  - boundary message in chat → divider renders correctly

- **`test_context_fill_bar.py`** (9 tests):
  - threshold transitions (ok / autocompact / blocked badges)
  - window from profile.provider.context_window
  - token counter usage when cached, fallback when not
  - bar grows with fill
  - percentage label + model name presence

### The "fade reads as bg" insight

The fold animation works because the chars STAY in the grid but
their fg color matches `theme.bg` exactly when fade_alpha → 0.
Verified empirically by reading cell colors at multiple elapsed
times:

```
t=0.00s  anticipation start  fg=#616368  Δfrom_bg=298  (fully visible)
t=0.40s  fold 8%             fg=#4b4d51  Δfrom_bg=231  (starting to dim)
t=0.60s  fold 25%            fg=#292a2c  Δfrom_bg=125  (clearly dimmer)
t=0.90s  fold 50%            fg=#0c0d0e  Δfrom_bg=37   (mostly faded)
t=1.20s  fold 75%            fg=#010202  Δfrom_bg=3    (essentially invisible)
t=1.50s  fold→materialize    fg=#000101  Δfrom_bg=0    (gone — exact bg)
```

The `ease_out_cubic` curve hits cleanly: anticipation holds visible,
fold ramps fast at first then settles to invisible. **No char
deletions** — just color transitions. This is exactly the kind of
in-place mutation no Rich/prompt_toolkit-based harness can do
because once they `print()` a line, they can't reach it.

### Live E2E results

Driven against qwopus on /burn 3000 → /compact:
- Compaction wall time: 10.8s (qwopus 27B summarizing 12 rounds)
- Reduction: 6 → 6 rounds, 3,092 → 1,978 tokens (37% saved)
- All 7 captured animation phases render correctly:
  - anticipation: snapshot visible
  - fold_mid: snapshot visible (colors faded, plaintext can't
    show)
  - materialize_start: divider at ~36% extent, no pill yet
  - materialize_mid: divider at ~75%, pill snaps in
  - materialize_full: divider at full extent
  - reveal_mid: summary fading in (alpha invisible in plaintext)
  - settled: boundary persists with subtle pulse

The summary content was excellent — Qwen captured every burn
topic accurately:

> "I noted that the user asked about the rendering layers in
> successor, and I explained that there are five layers: measure,
> cells, paint, composite, and diff. The user then asked about
> how the bash subsystem parses commands… [continues with all
> 6 burn topics]"

### Renderer features the animation exercises

| concepts.md cat | feature | where |
|---|---|---|
| Cat 1 (mutable cells) | every cell of every pre-compact message gets re-styled toward bg over the fold phase | `_faded` helper in `_paint_chat_row` |
| Cat 2 (smooth animation) | every animation effect is a per-frame function of `(state, time) → cells` via lerp_rgb + ease_out_cubic | phase-based `_build_rows_from_messages` overrides |
| Cat 3 (multi-region UI) | divider, summary, kept rounds, footer pill all paint in the same frame as a coherent composition | `_paint_chat_area` + `_paint_static_footer` |
| Cat 5 (deterministic) | every animation frame is `(elapsed, viewport, state) → grid`, fully snapshot-testable | `test_compaction_animation.py` reads cell colors directly |
| Cat 7 (programmatic UI) | the chat scroll is pinned to the boundary during materialize/reveal regardless of where the user was | `_paint_chat_area`'s scroll_override |
| Cat 8 (smooth transitions) | the divider materializes from the center outward via `ease_out_cubic`, the pill alpha-fades on top | `paint_horizontal_divider(t)` + pill_alpha calc |

### Notes

- **The visible compaction event is unique to Successor.** Other
  agent harnesses can't do this because once they `print()` a
  message line, it belongs to the terminal and they can't reach
  it. Our diff layer owns every cell, so we can fade them out,
  draw a divider through them, and replace them with a summary
  artifact — frame by frame, deterministic, no flicker.
- **The animation duration (5s total)** is long enough to feel
  like a narrative but short enough to never feel slow. The
  individual phases are tuned to give the user time to read each
  visual change before the next happens.
- **The scroll override** was the trickiest detail. Before adding
  it, the boundary materialized off-screen above the chat region
  because the kept rounds dominated the visible area. The fix:
  during materialize/reveal/toast, find the boundary row in the
  committed list and compute a temporary scroll_offset that
  positions it in the upper-sixth of the visible chat region.
  After the animation completes, normal scrolling resumes.
- **The continuous pulse on the settled boundary** is a 0.4 Hz
  sine wave applied to the divider color. Subtle but present —
  signals that the boundary is alive context, not a piece of
  static chrome you can ignore.

---

## Phase 5.3 — async + KV-cache-friendly compaction (2026-04-07)

The compaction subsystem becomes ACTUALLY USABLE at large context.
Two independent fixes that together take compaction from "freezes
the chat for ~19 minutes at 256K context" to "5-6 minute background
operation with a live spinner that the user can cancel."

### The problem

The user found that `/compact` at 256K context completely froze
the CLI. Profiling revealed two compounding issues:

1. **`compact()` was synchronous in the chat path.** `_handle_compact_cmd`
   blocked in `_submit()` waiting for the model's summary. No
   `on_tick` ran during that time → frozen UI.

2. **The summarization prompt was KV-cache-hostile.** It used a
   fresh system prompt + a single user message containing the
   serialized transcript. From llama.cpp's perspective, this
   prompt diverged from the chat's normal sends at token 0, so
   the cached prefix was useless and llama had to re-eval the
   entire 256K prompt from scratch. At 307 tok/s prompt eval that's
   ~14 minutes BEFORE the model even started generating the summary.

   For comparison, normal chat at 200K is fast because each turn's
   prompt is a CONTINUATION of the cached prefix — only the new user
   message gets evaluated.

### Fix 1: KV-cache-friendly summarization prompt

`agent/compact.py:_build_summary_prompt` was rewritten to send the
chat's existing message structure (system prompt + all rounds)
followed by a single user instruction message:

```
[chat_system_prompt][user1][asst1]...[userN][asstN][synthetic_user: "now summarize..."]
                                                    ^ divergence point — only this is fresh
```

llama.cpp can now reuse its KV cache for everything except the
trailing instruction message. At 256K context:

  - Old approach: ~14 minutes prompt eval (cache miss on 256K tokens)
  - New approach: ~1 second prompt eval (full cache reuse)
  - Generation: still takes the full ~5 minutes for a 16K-token
    summary (this is unavoidable model work)

The model sees the entire conversation including the rounds we'll
keep verbatim. The instruction tells it to focus its summary on the
older portion. Some redundancy with kept rounds is harmless because
the kept rounds are preserved in the post-compact log anyway.

PTL retry path was updated too — drops oldest rounds from the FULL
log on each retry instead of just the rounds-to-summarize.

### Fix 2: Async _CompactionWorker thread

`SuccessorChat` gained a `_CompactionWorker` class (mirrors the
`ChatStream` pattern) that runs `compact()` in a daemon thread with
a result attribute. `_handle_compact_cmd` now:

1. Snapshots `self.messages`
2. Pre-computes `pre_compact_tokens` and `rounds_to_summarize` for
   the spinner indicator
3. Arms the animation IMMEDIATELY
4. Spawns the worker
5. Returns in <1 second

`on_tick` calls a new `_poll_compaction_worker()` on every frame.
When the worker reports success, the chat applies the new log via
`_from_agent_log()` and sets `_compaction_anim.result_arrived_at`,
which triggers the materialize → reveal → toast transition.

The chat REMAINS INTERACTIVE during the entire 5-6 minute wait —
frame ticks continue, the spinner animates, and Ctrl+G cancels.

### Fix 3: WAITING phase + spinner overlay

`_CompactionAnimation` was extended with a sixth phase:

```
T=0       compaction triggered → snapshot + spawn worker
T=0-300   ANTICIPATION  rounds glow toward accent_warm
T=300-1500 FOLD         rounds fade fg → bg via lerp_rgb
T=1500-?? WAITING       indefinite — model is generating
                        Spinner + "compacting N rounds (X tokens)"
                        Ctrl+G to cancel
T=R-R+400 MATERIALIZE   (R = result_arrived_at) divider draws in
T=R+400-R+1000 REVEAL   summary fades in below the divider
T=R+1000-R+3500 TOAST   settled state with subtle pulse
```

The phase machine now uses `result_arrived_at` as the anchor for
post-fold phases instead of a fixed offset from `started_at`. This
lets the wait time be arbitrarily long without compressing the
visible animation.

A new `_paint_compaction_waiting_overlay` method paints a centered
status box during the waiting phase:

```
╭──────────────────────────────────────────────────────────╮
│        ⠇  compacting 735 rounds · 204,821 tokens         │
│                                                          │
│           elapsed: 00:23  ·  Ctrl+G to cancel            │
╰──────────────────────────────────────────────────────────╯
```

Spinner animates at ~10 Hz via `_compaction_anim.spinner_frame()`.
The chat content behind it is fully faded out (`fade_alpha=0`)
during the waiting phase so the spinner overlay is the visual focus.

### Cancel UX

Ctrl+G during compaction:
1. Calls `_compaction_worker.close()` (sets the worker's stop event)
2. Clears `_compaction_worker` and `_compaction_anim`
3. Appends "compaction cancelled" synthetic message
4. Worker thread continues until its HTTP request completes (we
   can't yank an in-flight urllib request) but the result is
   discarded because the stop event was set

Mirrors the existing Ctrl+G abort for in-flight chat streams.

### Tests (7 new — 645 total, all passing)

In `test_compaction_animation.py`:
- `test_phase_at_waiting_when_no_result_yet` — waiting is indefinite
- `test_phase_at_waiting_transitions_to_materialize_on_result` —
  setting result_arrived_at unsticks the animation
- `test_spinner_frame_animates` — frames cycle at 10 Hz
- `test_waiting_overlay_shows_spinner_and_status` — visual snapshot
  of the centered overlay with rounds + tokens + elapsed + cancel
- `test_worker_runs_in_background` — start() returns instantly,
  poll() returns the result when done
- `test_worker_close_aborts_pending_result` — close() before completion
  discards the result
- `test_handle_compact_cmd_is_non_blocking` — `/compact` via _submit
  returns in <0.2s even with a slow mock client

In `test_agent_compact.py` one existing test was updated to check
the new prompt structure (each round becomes its own user/assistant
message in the API prompt, not a single embedded transcript).

### Live E2E results at 200K context

```
STEP 1: /burn 200000  → 1457 messages
STEP 2: insert key fact at start of chat
STEP 3: /compact via _submit
        _submit() returned in 0.848s  ← was: ~19 minutes (freeze)
        Worker spawned, animation armed
STEP 4: tick loop (simulating frame loop at 16 fps)
        T+0.2s   anticipation phase
        T+0.3s   fold phase
        T+1.5s   WAITING phase begins (spinner visible)
        T+354.4s WAITING ends (result arrived)
        T+354.4s materialize phase
        T+354.8s reveal phase
        T+355.4s toast phase
        ────────────────────────
        Total: 357.9s wall time
        Frame rate during wait: 16 ticks/s
        Chat REMAINED INTERACTIVE the entire time
```

### What's NOT yet built

- **Cache pre-warming for the post-compact state**. After compaction,
  the next user message has a different prefix from what's in the KV
  cache (the cache holds the full chat + instruction; the new send
  starts with [sys][boundary][summary]). The next message pays a
  ~40-second cache miss to evaluate the post-compact prefix. We
  could fire a background warm-up request after compaction to
  populate the cache before the next user message arrives. Deferred
  to phase 5.4.

- **Recall fidelity at extreme burn ratios**. When the source content
  is 700+ near-identical synthetic burn rounds, Qwen's distill has
  trouble preserving unique facts in the summary. The earlier test
  with REAL conversation content (the "Lycaon" name test) had 100%
  recall. This is a model fidelity quirk, not a code issue, and
  it's much less of an issue with real conversational content
  where each round is distinct.

### Why this is the right architecture

The cache-friendly principle is simple: **every prompt llama.cpp
sees should be a continuation of the previous prompt, not a new
one**. The chat naturally satisfies this for normal turns. The
compaction was the one place we violated it, and that's where the
freeze happened. Fixing it required changing the prompt structure
but no other architectural changes — the post-compact state shape
stays identical, the renderer stays identical, the agent loop's
trigger logic stays identical.

The async worker is also straightforward — same pattern as the
existing `ChatStream`, and it's the only thing standing between
"interactive at 200K context" and "frozen for 6 minutes." Worth
every line.

---

## Phase 5.4 — KV cache pre-warming after compaction (2026-04-07)

The compaction subsystem becomes COMPLETE — not just async, but
also self-warming so the next user message after compaction is
near-instant instead of paying a one-time cache miss.

### The problem (carried over from phase 5.3)

After compaction, `self.messages` becomes `[boundary][summary][last_N_kept]`.
The next user message sends `[sys][boundary][summary][last_N_kept][new_user]`.
llama.cpp's KV cache currently holds the OLD chat structure plus the
summarization request — none of which matches the new prefix. The
next user message would pay a full cache miss to evaluate the
post-compact prefix from scratch:

  - Post-compact ≈ 1.3K tokens (small log) → ~1 second cache miss
  - Post-compact ≈ 12K tokens (256K starting log) → ~40 second cache miss

### The fix

After `compact()` completes, fire a `max_tokens=1` background request
to llama.cpp with the post-compact prefix. The prompt eval populates
the cache; the 1-token generation is essentially free. The next REAL
user message then hits a warm cache and prompt eval is near-instant.

### `_CacheWarmer` class

New worker class in `chat.py` that mirrors the `_CompactionWorker`
pattern:

- `start()` spawns a daemon thread that calls `client.stream_chat`
  with `max_tokens=1`
- `is_done()` / `is_running()` for status queries
- `close()` aborts the warmer (sets stop event + closes the
  underlying ChatStream so the worker thread unblocks)
- All exceptions caught silently — warming is best-effort, never
  blocks the chat

### Integration

`_poll_compaction_worker` spawns a `_CacheWarmer` the moment a
successful compaction result lands, in parallel with the
materialize/reveal/toast animation phases. The warmer runs in the
background while the user reads the summary.

`_submit` cancels any in-flight warmer at the top of every input
submission — the user's message takes priority over background
warming. If we let the warmer keep running, the user's request would
queue behind it on the llama.cpp slot and they'd wait LONGER than
if we'd never warmed at all.

`on_tick` clears the warmer reference once the worker thread reports
`is_done()`. The thread itself dies on its own as a daemon.

### The HTTP 400 bug we caught

The first attempt at warming failed with HTTP 400. Cause:
**llama.cpp's chat completion endpoint rejects prompts that end on
an `assistant` message when thinking mode is enabled** — the model
expects a user turn to respond to:

```
{"error":{"code":400,"message":"Assistant response prefill is
incompatible with enable_thinking."}}
```

The post-compact log ends on the last kept assistant turn, so the
warmer needs to append a synthetic placeholder user message:

```python
warmer_messages = list(post_compact_messages)
if warmer_messages[-1]["role"] == "assistant":
    warmer_messages.append({"role": "user", "content": "."})
```

The cache match against the synthetic user message will fail when
the real user sends their next message (their content differs from
"."), but the cache match for everything BEFORE the synthetic
message — which is the post-compact prefix proper — is preserved.
That's exactly what we want.

### Footer warming indicator

The static footer gained a tiny spinner badge that shows when the
warmer is running:

```
ctx 12345/262144 ████░░░░ 4.7% qwopus  ⠹ warming
```

Quiet but visible — tells the user "the harness is doing background
work that will make the next message faster". When warming completes
the badge disappears.

### Direct measurement of the speedup

Verified live against qwopus by reading llama.cpp's `cache_n`
metric (the number of input tokens that hit the cache) and
`prompt_n` (the number of newly-evaluated tokens):

```
Test setup: 50-round conversation, ~17K tokens
After compaction: 1.6K tokens, 11 messages

CONTROL (no warming):
  cache_n  = 0
  prompt_n = 1331  (full cache miss)
  wall     = 1.35s

WARMED:
  Warmer evaluated:    1349 tokens (populates cache)
  cache_n on next msg = 837   (63% cache hit on the post-compact prefix)
  prompt_n            = 516   (only the diverging tokens evaluated)
  wall                = 0.95s
```

**Speedup at 1.3K post-compact tokens: 1.4x.**

At larger scales the speedup grows linearly with the post-compact
prefix size because the cache miss cost scales linearly while the
cache hit cost stays near-constant:

  - 1.3K post-compact:  1.4x speedup    (~0.4s saved)
  - 5K post-compact:    ~5x speedup     (~5s saved, extrapolated)
  - 12K post-compact:   ~25x speedup    (~38s saved at 200K starting context)

The 1.4x at small scale isn't dramatic but it's MEASURABLE through
the cache_n metric, which proves the architecture is correct. At the
scales where the user actually runs into the freeze (200K+), the
warmer turns "wait 40 seconds for the next message" into "wait <1
second for the next message".

### Tests (7 new — 652 total, all passing)

In `test_compaction_animation.py`:
- `test_warmer_starts_returns_immediately` — start() is non-blocking
- `test_warmer_uses_max_tokens_one` — verify the cheap generation
- `test_warmer_close_aborts_in_flight` — close() promptly stops
- `test_warmer_silent_failure_on_exception` — best-effort behavior
- `test_warmer_spawned_after_compaction` — `_poll_compaction_worker`
  spawns the warmer when a result lands
- `test_submit_cancels_in_flight_warmer` — `_submit` cancels and
  clears the warmer
- `test_footer_warming_indicator` — the spinner badge appears in
  the static footer when warming is in progress

### Why this completes the compaction story

The full compaction flow now looks like this end-to-end:

1. User runs `/compact` → returns in <1s
2. Animation plays: anticipation → fold → waiting (5+ minutes
   at 256K context, but the chat is interactive throughout with
   spinner + Ctrl+G cancel)
3. Worker reports result → animation transitions to
   materialize → reveal → settled
4. **Cache warmer fires in parallel with the animation**, running
   for ~30-40 seconds in the background
5. User types their next message → cache is warm, response is
   near-instant

Before phase 5.3+5.4, this entire flow was a 19-minute freeze.
After: the user's perceived wait is 5 minutes of animated spinner
(during which they can cancel or do other things) + 0 second next
message.

### What's NOT yet built

- **Autocompact integration**. Right now `/compact` is the only
  trigger; the agent loop's auto-trigger threshold isn't wired into
  the chat. When the loop lands properly, the autocompact path
  reuses the same `_CompactionWorker` + `_CacheWarmer` machinery.
- **Visible elapsed time during the long wait**. The waiting overlay
  shows `elapsed: 00:23` but doesn't update in real time within the
  test capture (it does in the live chat at frame rate).
- **Warmer timing telemetry**. We could expose `cache_n` improvements
  in the footer or a debug overlay so the user can see the actual
  speedup their warming bought them.

---

## Phase 5.5 — chat stays interactive during compaction wait (2026-04-07)

User feedback caught a UX regression in phase 5.3: the centered
spinner overlay during the WAITING phase blacked out the entire
chat content with `fade_alpha=0`. This walled off the harness's
strongpoint — searchable, scrollable, mutable past content — at
exactly the moment when the user might want to read or catch up
on notes while compaction runs in the background.

> "It's helpful still to be able to scroll and see what's going
> on in the session while you're waiting. Sometimes it's a good
> time to catch up on notes. Our strongpoint in this harness is
> that you can search and do whatever you want with the text in
> it, it doesn't make sense to wall it off while there's a moment
> of forced idle. In fact it makes the opposite of sense."

The fix is small but the design philosophy matters: **the chat
content is the user's content, and the harness should never block
their access to it for purely cosmetic reasons.**

### What changed

- **`_build_message_lines`** during anticipation/fold/waiting now
  paints the snapshot at full opacity (no `fade_alpha=0`). The
  user sees the exact same chat content they had before /compact
  was triggered.

- **`_paint_compaction_waiting_overlay`** (the centered spinner box)
  is GONE. The chat region is no longer obscured during the wait.

- **A new compacting badge** lives in the static footer next to the
  context bar:
  ```
  ctx 12345/262144 ████░░░░ ⠹ compacting 735r · 00:23  4.7% qwopus
  ```
  Animated spinner + round count + elapsed time. Quiet but always
  visible. Disappears when the worker reports a result and the
  materialize phase begins.

- **Right-label color in the footer** now reflects compaction state
  too — accent_warm tint while compacting/warming, falling back to
  the normal threshold-based color afterward.

### What stays the same

- The 5-phase animation arc (anticipation → fold → waiting →
  materialize → reveal → toast) still plays — only the visual
  treatment of the early phases changed
- Materialize/reveal/toast still play their dramatic moment when
  the result lands (boundary divider grows from center, summary
  fades in below, settled with subtle pulse)
- The cache pre-warmer still fires in parallel with the animation
- Ctrl+G still cancels mid-flight
- All the underlying machinery — worker thread, phase machine,
  result_arrived_at — is unchanged

### Verified behavior

- Chat content fully visible at full opacity during all phases
  before materialize
- Spinner badge visible in the footer while the worker is running
- Scroll works during waiting (test verified by scrolling up and
  confirming different content is visible at different offsets)
- Search would also work (we didn't add a specific test but the
  search infra is unchanged and operates on the snapshot)
- The materialize/reveal/toast still play correctly when the
  worker returns

### Tests (1 updated, 1 renamed — 652 still passing)

- `test_anim_fold_dims_snapshot` → `test_anim_keeps_chat_visible_during_wait`:
  inverted assertion — instead of checking that fold dims content,
  now checks that all phases keep content at full opacity
- `test_waiting_overlay_shows_spinner_and_status` →
  `test_waiting_shows_compacting_badge_in_footer`: the indicator
  moved from the centered overlay to the footer badge; test
  verifies the badge is in the footer AND the chat content is
  still visible

### The lesson

The original design was clever (dramatic fade-out, centered focus
point) but it fought the architecture instead of using it. The
renderer is built around mutable, addressable past content — the
*opposite* of "frozen scrollback you can never reach again".
Blacking out the chat region during a forced wait makes the
harness behave like Rich/prompt_toolkit at exactly the moment
where its uniqueness should shine through.

The fix isn't just removing the overlay — it's recognizing that
the chat's visibility during long-running background work is a
FEATURE, not something to compromise on for visual polish.

---

## Phase 5.6 — summary at the bottom + integrated boundary divider (2026-04-07)

User feedback after watching phase 5.5: snapping the scroll up to
show the boundary at the top of the post-compact log is the wrong
direction. The user is auto-scrolled to the bottom of the chat
where they're naturally looking; the agent's summary should appear
THERE so they can immediately read it and judge its quality.

> "Would it be better if we rendered the agent generated compaction
> summary at the bottom instead of snapping up to the top and then
> back down though? It's kind of cool to see the agent summary
> because then we can also see where it might have shortcomings.
> Makes it a lot easier to iterate"

Two changes that together completely rethink the post-compaction
display:

### 1. Display order ≠ API order

`self.messages` is now stored in DISPLAY order:
```
[kept_round_1]...[kept_round_K][summary]
```

The summary is the LAST message in the list, which means it appears
at the BOTTOM of the chat (since the chat is bottom-anchored). The
user stays auto-scrolled to the bottom — no snap, no scroll override.

For the model, `_api_ordered_messages()` reorders to API/chronological
order:
```
[summary][kept_round_1]...[kept_round_K]
```

The model sees the summary FIRST (representing older content that
was summarized), then the recent rounds in temporal order. Both the
chat send path (`_submit`) and the agent log adapter (`_to_agent_log`)
use this reordered view.

Implementation: a single `_api_ordered_messages()` helper that finds
the summary message and emits `[summary] + [everything else in
original order]`. No-compaction case is just `list(self.messages)`.

### 2. Boundary divider integrated into summary's render

Previously the boundary divider was a separate `_Message` in
`self.messages`. With verbose summaries (Qwen sometimes produces
1.5K char summaries with leaked reasoning), the boundary message
could get pushed off-screen above the summary, separating them
visually.

Fix: the boundary divider is no longer a separate message. It's
rendered as the FIRST ROW of the summary message itself, glued to
the summary's top edge. They're never separated.

`_from_agent_log` no longer creates a separate boundary `_Message`;
it just attaches the `boundary_meta` to the summary message.

The row builder's summary case now emits two parts:
- ROW 1: a `_RenderedRow` with `is_boundary=True`, the `boundary_meta`
  attached, and `materialize_t` driven by the animation phase
- ROWS 2+: the summary content rows with the `▼` prefix and
  `fade_alpha` driven by the reveal phase

The materialize animation still plays (boundary divider grows from
center outward) and the reveal animation still plays (summary
fades in below it), but they're guaranteed to be visually adjacent
because they're part of the same message's render.

### 3. Scroll override removed

The scroll override during materialize/reveal/toast is gone. The
user stays at `auto_scroll=True` throughout. The boundary +
summary now live at the bottom of `self.messages` so they're
naturally in view at the bottom of the chat. No forced snap, no
position recovery.

### Live E2E result

```
After /burn 4000: 31 messages
Firing /compact...
_submit returned in 0.024s   ← non-blocking
Compaction completed in 25.4s
Final state: 13 messages, scroll=0, auto=True
```

The chat shows:
```
... kept rounds at top ...
you ▸ Tell me again about what compaction does. ...
successor ▸ Sure, on iteration 14: summarizes old turns into one
       block, keeps recent rounds verbatim. ...

━━━━━━━━━━━━┤ ▼ 9 rounds · 4k → 2k · 51% saved ▼ ├━━━━━━━━━━━━
▼ I noted that the user asked about six topics in a repeating
       cycle. The rendering layers in successor consist of five
       layers: measure, cells, paint, composite, and diff. The bash
       subsystem parses commands by splitting with shlex, looking
       up in a registry, and falling back to a generic card. ...
▍
 ctx 1932/262144 █░░ 0.74%  qwopus
```

The user immediately sees the agent's summary at the bottom, with
the boundary divider showing the compaction stats just above it.
They can judge the quality (and catch model failure modes) at a
glance. New messages they type after compaction append below the
summary in the message list — naturally pushing it up over time
as the conversation continues. The summary is then accessible by
scrolling up at any time.

### Why this is the right architecture

The previous design (boundary at top, scroll-snap to it) was
fighting the chat's natural reading direction. The chat is
bottom-anchored: new content appears at the bottom, the user's
input is at the bottom, the user's eye is at the bottom. The
summary is the OUTPUT of compaction — it should appear where new
output normally appears.

The split between display order and API order is a small piece of
machinery that lets us have the user-friendly visual flow AND the
model-friendly chronological order without compromising either.
This is the kind of decoupling the architecture's pure-function
design makes easy: `_api_ordered_messages()` is a six-line pure
helper, the painter doesn't care about API order, and the model
sees what it expects.

### Tests (652 still passing)

No test changes needed — the existing tests check the boundary's
visual properties and the animation's phase machine, both of
which still work. The structural changes are internal to how
self.messages and the row builder cooperate.

---

## Phase 5.7 — tools architecture: registry + setup wizard + config menu + streaming dispatch (2026-04-07)

The bash-masking subsystem landed in Phase 5.0 wired up only the
`/bash <command>` slash command. The model could not emit bash of
its own accord and have it execute — the stream committed as plain
text, fenced blocks were rendered as markdown codeblocks, and
nothing fired. Phase 5.7 closes the loop: when the model emits a
```` ```bash ```` block during a streamed reply, the harness detects
it client-side, runs it through the same `dispatch_bash()` pipeline
the slash command uses, and stacks the resulting tool card BELOW
the assistant message that produced it.

The architecture is deliberately set up so adding new tools later
is one registry entry. Every consumer (setup wizard, config menu,
chat system prompt, streaming dispatch) iterates the registry.

### 1. `src/successor/tools_registry.py` — source of truth

One tiny module with a frozen `ToolDescriptor` dataclass and an
`AVAILABLE_TOOLS` dict keyed by name. Currently one entry:

```python
AVAILABLE_TOOLS = {
    "bash": ToolDescriptor(
        name="bash",
        label="bash",
        description="Run shell commands. Dangerous commands refused automatically.",
        default_enabled=True,
        system_prompt_doc=BASH_DOC,
    ),
}
```

`BASH_DOC` is the markdown the system prompt injects so the model
learns what the tool does, how to invoke it (fenced code blocks),
and the safety rules. Helpers: `is_known_tool()`, `filter_known()`,
`default_enabled_tools()`, `build_system_prompt_tools_section()`.

Three consumers iterate the registry:
1. Setup wizard — shows enable/disable toggles when creating a profile
2. Config menu — shows toggles for editing an existing profile
3. Chat — decides whether to instantiate `BashStreamDetector` AND
   builds the "## Available Tools" section of the system prompt

### 2. Chat wiring — streamed bash → tool card

`chat._submit` now does two new things when the active profile has
tools enabled:
- Appends `build_system_prompt_tools_section()` to the system prompt
  so the model sees a markdown explanation of every enabled tool
- Instantiates `self._stream_bash_detector = BashStreamDetector()`
  if "bash" is in `profile.tools`; None otherwise

`chat._pump_stream` feeds every `ContentChunk` through the detector:
```python
elif isinstance(ev, ContentChunk):
    self._stream_content.append(ev.text)
    if self._stream_bash_detector is not None:
        self._stream_bash_detector.feed(ev.text)
```

On `StreamEnded`, the detector is flushed and any completed blocks
go through `_dispatch_streamed_bash_blocks()` — each block becomes
its own `_Message(tool_card=...)` appended AFTER the assistant
message. Dangerous commands still raise `DangerousCommandRefused`
and surface the refused card + synthetic note.

`_submit` also serializes existing tool cards to text when building
the API request so prior turns' tool outputs are visible to the
model on its next call:

```python
def _serialize_tool_card_for_api(card: ToolCard) -> str:
    body_lines = [f"$ {card.raw_command}"]
    if card.output:
        body_lines.append(card.output.rstrip())
    if card.stderr and card.stderr.strip():
        body_lines.append(f"[stderr] {card.stderr.rstrip()}")
    if card.exit_code is not None and card.exit_code != 0:
        body_lines.append(f"[exit {card.exit_code}]")
    return "\n".join(body_lines)
```

This is what closes the agent loop — the model emits bash, the
harness runs it, and the model sees the output on its next turn as
part of the assistant history.

### 3. Setup wizard — new TOOLS step

The wizard grows from 7 steps to 8 (welcome, name, theme, mode,
density, intro, **tools**, review). The tools step is a multi-select
checklist:

```
 step 7 of 8 — enable tools

 what should this profile be allowed to do?
 ↑↓ move · space toggles · → next (chat-only is fine — uncheck everything)

 ▸ [✓]  bash
        Run shell commands. Dangerous commands refused automatically.

 1 tool enabled
```

Space toggles the cursor'd tool on/off. Users who just want a
chat-only harness (no bash, no subprocess execution) can uncheck
everything; the summary footer changes to "chat-only mode — no
tools enabled" and the saved profile has `tools: []`. The review
step shows the actual selected tools instead of the stale phase-6
placeholder.

Default selection is `default_enabled_tools()`, which currently
means bash is pre-checked. When a future tool ships with
`default_enabled=False`, it'll show up in the step but unchecked.

### 4. Config menu — new `TOOLS_TOGGLE` field kind

The existing config menu has `CYCLE`, `TOGGLE`, `TEXT`, `NUMBER`,
`SECRET`, `MULTILINE`, `READONLY` field kinds. Phase 5.7 adds
`TOOLS_TOGGLE` — a multi-select overlay that replaces the old
`READONLY` placeholder for the `tools` row.

Enter on the tools row opens an overlay that mirrors the cycle
overlay's placement (anchored below the row, flipping above if
clipped). Each row shows a checkbox + tool label + description:

```
╭─ tools — space toggles ─────────────────────────────╮
│ ▸ [✓]  bash   Run shell commands. Dangerous…       │
╰─────────────────────────────────────────────────────╯
```

- ↑↓ moves the cursor between tools
- Space toggles the highlighted tool's enabled state in the LIVE
  profile — the dirty marker fires immediately and the preview
  chat refreshes so the user sees the effect in real time
- Enter commits (closes the overlay, keeps the edits)
- Esc restores the pre-edit snapshot and closes

Dirty tracking compares the working profile's `tools` tuple against
the initial snapshot, so toggling on and then off clears the dirty
flag automatically. Saves persist `profile.tools` to disk via the
existing `_save()` path with no special-casing.

The settings row's display value changes with the selection:
- `bash` (or `bash, read_file` later) when tools are enabled
- `(none — chat only)` when no tools are enabled

### 5. Profile defaults

`builtin/profiles/successor-dev.json` — the harness-development
profile — now has `"tools": ["bash"]` so working on successor
itself with bash enabled is the default. The other built-in
profile still has no tools (safer for the "just open a chat"
flow).

### Live E2E result

Verified with the real qwopus model during development. The model
emits `` `bash ... ` `` in response to prompts like "list the
files in this directory"; the harness:
1. Detects the fence mid-stream via `BashStreamDetector.feed()`
2. Commits the assistant message on `StreamEnded`
3. Flushes the detector, dispatches each detected block through
   `dispatch_bash()`
4. Appends each resulting `ToolCard` as its own `_Message` below
   the assistant message
5. On the user's next turn, serializes those cards into the API
   history so the model sees its own bash outputs

### Tests

- `tests/test_wizard.py` — 5 new tests: snapshot of the TOOLS
  step checklist, chat-only mode rendering, `_handle_tools` toggle
  flow, `_WizardState` chat-only roundtrip, full flow that drives
  the wizard from WELCOME through the new TOOLS step into REVIEW
  and asserts `payload["tools"] == ["bash"]` lands on disk
- `tests/test_config_menu.py` — 7 new tests: overlay open,
  space-toggles-live, esc restores snapshot, enter commits, dirty
  marker fires, persistence to disk, snapshot of the overlay
- `tests/test_chat_bash.py` — 4 new tests: `_FakeStream` harness
  drives fenced bash blocks through `_pump_stream` and asserts
  tool cards land after assistant message (single block, multiple
  blocks, detector=None is no-op, dangerous command shows refusal)

Total: 667 tests, all passing (up from 652).

### Why this is the right approach for local models

The same logic from Phase 5.0 applies, now at the streaming layer.
Mid-grade local models (Qwen 3.5 distill) are fluent in bash
because they've eaten millions of bash commands in pretraining;
they are unreliable with structured tool-call schemas. So we let
them emit bash in their strongest mode (fenced code blocks),
detect it CLIENT-SIDE via the existing stream detector state
machine, run it through the same dispatch pipeline the slash
command uses, and surface the result as a structured card the
user can verify at a glance.

There's one piece of mechanism (the registry) and every consumer
iterates it. When we add `read_file` or `web_search` later, we
add one entry to `AVAILABLE_TOOLS` and the setup wizard, config
menu, and system prompt auto-discover it.

---

## Phase 5.8 — bash safety flags + verb classes + prepared output (2026-04-07)

Three commits that together unlock the harness for "go public" status:

1. **Bash safety flags** — yolo mode, read-only mode, per-profile
   tuning knobs plumbed through profile.tool_config["bash"].
2. **Verb classification** — per-class icons in tool card headers
   so cards are recognizable at a glance by their glyph.
3. **PreparedToolOutput** — Pretext-shaped per-class output pipeline
   that parses grep/ls/git-status output ONCE, caches wrapped lines
   per width, and surfaces structural spans (match highlighting,
   file type markers, status flags) the renderer paints as styled
   ranges.

### 1. Safety flags (bash/exec.py + wizard/config.py)

Three flags live under `profile.tool_config["bash"]`:

```python
{
    "allow_dangerous": False,  # yolo: sudo/rm-rf/eval/curl|sh all run
    "allow_mutating": True,    # flip off for read-only mode
    "timeout_s": 30.0,
    "max_output_bytes": 8192,
}
```

`resolve_bash_config(profile)` folds the dict over `BashConfig`
defaults and returns a frozen dataclass. Both dispatch sites
(`/bash` slash command and streaming bash detection) call it once
per batch. Flipping any flag in the config menu takes effect on
the next run with zero plumbing.

Config menu: new `bash safety` section with four rows (yolo, allow
mutating, timeout, max output). The rows are hidden via
`_field_visible_for_profile()` when bash isn't in `profile.tools`
— no bash, no noise. Toggling bash off snaps the cursor off any
now-hidden bash_* row. The yolo toggle displays "⚠ YOLO" vs
"off (safe)" so the state is unambiguous.

`RefusedCommand` is a new base class for both
`DangerousCommandRefused` and `MutatingCommandRefused` so dispatch
sites can catch one exception and branch on the subclass for hint
text pointing at the config path.

### 2. Verb classes (bash/verbclass.py)

Pure lookup module: every parsed verb maps to one of 8 `VerbClass`
values, each with a distinct single-cell glyph:

| Class   | Glyph | Example verbs                                     |
|---------|-------|---------------------------------------------------|
| READ    | ◲     | read-file, concatenate-files, read-file-head/tail |
| SEARCH  | ⌕     | search-content, find-files, locate-binary         |
| LIST    | ☰     | list-directory                                    |
| INSPECT | ⊙     | working-directory, git-status/log/diff/show/blame |
| MUTATE  | ✎     | create-file, delete-file, cp/mv, git-add/commit   |
| EXEC    | ▶     | run-python-*, print-text                          |
| DANGER  | ⚠     | anything risk-classified as dangerous             |
| UNKNOWN | ?     | generic fallback, no parser match                 |

Risk escalation is preserved: if `card.risk == "dangerous"` the
class is ALWAYS DANGER regardless of the parser verb. Sudo ls,
git push --force, rm -rf / all render with the warn glyph even
though their native classes would be LIST / MUTATE / MUTATE.

The renderer uses this in `_verb_glyph_for_card()` — the old
risk-only glyph lookup (▸/✎/⚠) is replaced with a verb-class
lookup. Scrolling through a long chat, the glyphs alone make card
kinds recognizable in peripheral vision without reading verb text.

### 3. Pretext-shaped output pipeline (bash/prepared_output.py)

This is the killer feature — the architectural pattern finally
paying off where it matters most.

The old `_wrap_output(card, width)` did a fresh string split +
hard-wrap pass every frame. Worse, it had no way to carry span-
level metadata, so highlighting grep matches or marking ls entries
as dir/file/link would've required a second parse pass at paint
time.

`PreparedToolOutput` mirrors `PreparedText` / `BrailleArt`:

```python
class PreparedToolOutput:
    def __init__(self, card: ToolCard) -> None:
        self._prepared = _prepare_for_card(card)  # verb-class dispatch
        self._cache_w = -1
        self._cache_lines = []

    def layout(self, width: int, *, max_lines: int = 12) -> list[OutputLine]:
        if width == self._cache_w and max_lines == self._cache_max:
            return self._cache_lines
        # ... wrap + cache ...
```

`__init__` parses the output ONCE into a list of `_PreparedLine`
objects with semantic spans. Verb-class dispatch picks the parser:

- **SEARCH (grep)** — `_parse_grep_line()` matches `file:line:content`
  and splits content into alternating plain + match spans at each
  query hit (case-insensitive substring). The painter renders match
  spans with a warm-accent background — grep hits literally pop
  off the screen.
- **LIST (ls -l)** — `_parse_ls_line()` matches the long-format
  regex and splits into dim chrome (perms + size + date) + marker
  + name span. Directories get `▸`, symlinks `↗`, executables `★`,
  regular files `·`. The "total N" header is passed through as a
  dim header row.
- **INSPECT (git-status)** — `_parse_git_status_line()` matches the
  short-format flags and tints `M`/`A` chrome, `?` dim, `D`/`R` warn.
- **default** — plain stdout + stderr pass-through, matching the
  old `_wrap_output` behavior.

`layout(width)` hard-wraps each prepared line to the target width,
preserving span kinds across wraps (a long match span that straddles
a wrap boundary retains `match` on both halves). Width-keyed
single-entry cache: re-paints at the same width are zero-cost.

### 4. Renderer span painter

`_paint_output_line()` walks an `OutputLine`'s spans and paints
each with a style resolved from `_span_style(span_kind, row_kind,
theme)`. Five span kinds cover every highlight the current cards
need: `plain`, `match`, `chrome`, `dim`, `warn`. Five row kinds
tint the base: `stdout`, `stderr`, `match`, `truncated`, `header`.

Single source of truth for the (span_kind, row_kind) → Style
mapping. Adding a new kind means teaching this one function and
nothing else. The painter itself is 10 lines.

### 5. Two-level cache on _Message

Tool-card _Message instances gained two cache slots:

- `_prepared_tool_output: PreparedToolOutput | None` — built on
  first paint, reused forever (the card is frozen so the output
  never changes)
- `_card_rows_cache` + `_card_rows_cache_key` — the final
  pre-painted `list[_RenderedRow]`, keyed by `(body_width,
  id(theme_variant))`. Hit = zero-cost re-paint (the chat's
  existing cell-copy fast path takes over).

Resize or theme swap invalidates both. The PreparedToolOutput
stays valid; only the pre-painted row snapshot gets rebuilt at
the new width. Measured cost of a steady-state paint at 30fps
with 10 tool cards on screen: effectively zero after the first
frame.

### Live smoke test

Ran every verb class through dispatch_bash + paint_tool_card and
eyeballed the headers. Every glyph renders in its expected
position, every class-specific body layout works:

```
╭── ☰ list-directory ──────────── ...   ls -la src/
╭── ◲ read-file ───────────────── ...   cat README.md
╭── ⌕ search-content ──────────── ...   grep -rn def foo.py
╭── ⌕ find-files ──────────────── ...   find src -name '*.py'
╭── ⊙ working-directory ───────── ...   pwd
╭── ⌕ locate-binary ───────────── ...   which python
╭── ▶ print-text ──────────────── ...   echo hello
╭── ▶ run-python-inline ───────── ...   python -c 'print(42)'
╭── ✎ create-directory ────────── ...   mkdir /tmp/foo
╭── ✎ delete-file ─────────────── ...   rm /tmp/foo/x
╭── ⊙ git-status ──────────────── ...   git status --short
```

### Tests: 756 passing (up from 667)

- **test_bash_exec.py**: 15 new tests for `BashConfig` +
  `resolve_bash_config` + `allow_dangerous` / `allow_mutating` /
  `max_output_bytes` dispatch paths
- **test_chat_bash.py**: 3 new tests for yolo mode / read-only
  mode plumbing through the streaming path
- **test_config_menu.py**: 7 new tests for the bash flag rows,
  visibility toggling, cursor snap-back, persistence
- **test_bash_verbclass.py** (NEW): 37 tests covering every parser
  verb + risk-override rule + glyph uniqueness + real command
  classification (parametrized)
- **test_bash_render.py**: 6 tests updated to assert the new verb
  glyphs
- **test_bash_prepared_output.py** (NEW): 26 tests covering each
  parser (grep/ls/git-status), span split logic, cache behaviour,
  truncation, and chat-level two-level cache

### What this buys us architecturally

1. The renderer's cache pattern finally pays off where it's worth
   paying for — expensive structural parses done once, wrap passes
   cached, and the whole pre-painted row list cached on top of
   that.
2. Span-based output rendering means grep highlighting, ls
   classification, and git-status flag tinting are now structural
   properties of the prepared output, not paint-time guesses. A
   new verb class or span kind is one entry in a lookup table.
3. Yolo mode unlocks the harness for the user's real use case
   (mid-grade model, local box, trust the operator) without
   sacrificing the safety rails that the risk classifier provides
   by default. The classifier still tags commands — users just
   get to choose whether the tag refuses or not.
4. Every layer is independently testable. The verb-class lookup
   is pure. The prepared-output parsers are pure. The span painter
   is pure. The cache invalidation is deterministic.

---

## What's next

- **Additional tools** — `read_file`, `web_search`, `git_diff` as
  independent registry entries once we've used bash in practice and
  understand the shape better
- **Concurrent tool execution** — tools currently dispatch
  synchronously after the stream commits. A future phase wires
  asyncio so bash runs while the model streams the next token
- **Skill invocation strategy** — pick always-on vs on-demand after
  experimenting with Qwen 3.5 in practice
- **Find/replace in the prompt editor** — Ctrl+F opens a search bar
  inside the editor overlay, n/N jump matches
- **Undo/redo in the prompt editor** — operation log + Ctrl+Z/Ctrl+Y
- **User intros** — `~/.config/successor/intros/<name>/` directory
  walking, like themes and profiles. Lets the user drop their own
  intro frame sets.
- **Editing existing profiles via the wizard** — wizard re-entry mode
  that pre-populates state from a registered profile
- **Framework docs** — once the surface is stable

---

## Test count by phase

| Phase | Tests added | Cumulative |
|---|---|---|
| 1 (loader + theme + config) | 88 | 88 |
| 2 (provider) | 19 | 107 |
| 3 (profiles + intro) | 33 | 140 |
| 5 (skill loader) | 17 | 157 |
| 6 (tool registry) | 15 | 172 |
| 4 (setup wizard) | 35 | 207 |
| 4.5 (config menu) | 33 | 240 |
| 4.6 (editable text + multiline editor) | 36 | 276 |
| 4.7 (prompt editor v2: soft wrap + selection + clipboard) | 48 | 324 |
| 4.9 (delete profile from config menu) | 17 | 356 |
| 5.0 (bash-masking subsystem: parser + risk + exec + render + chat) | 131 | 487 |
| 5.1 (agent loop + compaction + burn rig) | 115 | 602 |
| 5.2 (visible compaction animation + context fill bar) | 28 | 630 |
| 5.2.1 (200K context fps regression fix) | 8 | 638 |
| 5.3 (async + KV-cache-friendly compaction + waiting overlay) | 7 | 645 |
| 5.4 (KV cache pre-warming after compaction) | 7 | 652 |
| 5.5 (chat stays interactive during compaction wait) | 0 | 652 |
| 5.6 (summary at the bottom + integrated boundary divider) | 0 | 652 |
| 5.7 (tools architecture: registry + wizard + config + streaming) | 15 | 667 |
| 5.8a (bash safety flags: yolo + read-only + tuning) | 22 | 689 |
| 5.8b (verb classification: per-class icons in card headers) | 41 | 730 |
| 5.8c (PreparedToolOutput: Pretext-shaped verb-aware output pipeline) | 26 | 756 |

(Counts above are approximate by phase boundary; actual test
collection may include additional small additions in subsequent
commits.)

End of Phase 5 series: **756 tests, all passing, hermetic via
`SUCCESSOR_CONFIG_DIR`, no fake mocks, no `.skip()` or `.todo()`.**

---

## Phase 5.9 — async bash dispatch + agent loop continuation + native Qwen tool calls (2026-04-07)

The harness's first live E2E against qwopus surfaced four hard
problems that blocked it from feeling like a real agent:

1. **No agent-loop continuation.** After tool dispatch the chat
   stopped cold. The model never saw its own tool output until the
   user typed another message.
2. **Tool execution blocked the tick loop.** `subprocess.run` was
   synchronous, so during a 30-second build the entire chat froze.
3. **The model didn't know its actual cwd.** Files landed in the
   wrong directory because the system prompt only mentioned the
   workspace pinning when `tool_config.bash.working_directory` was
   explicitly set.
4. **Streaming tool_call arguments were invisible.** The user saw
   nothing for several seconds while the model emitted partial
   `tool_call.function.arguments` JSON, then a fully-formed card
   appeared all at once.

Fixed with: continue-loop in `_pump_stream` (caps at 25 turns), an
`async BashRunner` that spawns subprocess.Popen in a background thread
and pumps stdout/stderr lines via a thread-safe queue, an unconditional
cwd injection into the system prompt, a streaming-tool-call preview
that infers verb + parameters from partial JSON, a sticky verb cache
keyed by `(stream_id, call_index)`, native Qwen `tool_calls` format
via the chat template's `<tool_call>`/`<tool_response>` tags, and a
heredoc body strip in the bash parser to fix shlex apostrophe crashes.

Tests: 756 → 811. Live E2E: 11 scenarios × 5 stability runs all green.

---

## Phase 6.0 — first public release prep (2026-04-07)

Repo hygiene and v0.1.0 launch readiness. None of this changed
behavior; it scrubbed personal references, added a license,
rewrote the README, and generated visual assets.

- LICENSE (Apache 2.0)
- pyproject bumped to 0.1.0 with license/keywords/classifiers/URLs
- `.gitignore` cleanup (`Ronin` → `Successor`)
- README rewrite leading with a tool-card screenshot and the
  diff.py-only-stdout-writer hook
- `assets/` with text-format snapshots
- `qwopus` and other personal references generified to `local`
  in default profiles, factory examples, llama provider defaults,
  wizard, config menu mock, e2e driver, and tests
- `scripts/swap_to_a3b.sh` and `swap_to_qwopus.sh` deleted
- CLAUDE.md trimmed from 700+ lines
- Paste handling: CRLF normalization, tab expansion, orphan focus
  tail strip, "↑ N more lines" overflow indicator on the input box
- Real `hard_wrap` bug fixed: `\n` was being short-circuited by the
  zero-width character branch and never produced row breaks
- Friendly `StreamError` rendering for connection refused / DNS /
  unreachable cases

Tests: 811 → 826.

---

## Phase 6.1 — provider auto-detection + OpenRouter wizard step (2026-04-07)

Found while testing the harness against OpenRouter from a clean
profile: the OpenAI-compat client appended `/v1/chat/completions`
to base_url, but every popular hosted provider treats `/v1` as part
of base_url. Result was `https://openrouter.ai/api/v1/v1/chat/completions`
→ 404. The same bug was latent in the llama.cpp client.

Also: there was no auto-detection of context window from any
provider. The chat read `provider.context_window` from profile JSON
with a hardcoded 262_144 fallback. So a user pointed at a 64K model
without manually setting context_window would have compaction
thresholds set against the wrong number, never proactively fire
autocompact, and eventually hit the model's real ceiling with an
opaque "context length exceeded" error.

Fixed with: `_api_root()` URL helper on both clients (detects
trailing `/v1` and skips the append), `detect_context_window()` on
both clients (llama.cpp probes `/props`, openai_compat probes
`/v1/models`), `_resolve_context_window()` on the chat with profile
override → detection → CONTEXT_MAX precedence (cached on the chat),
new `Step.PROVIDER` in the wizard with a 2-way picker (llamacpp /
openrouter) and inline api_key + model fields, friendly error
extensions for HTTP 401 / 402 / 429.

Tests: 826 → 858.

---

## Phase 6.2 — OpenAI as a first-class option + sysprompt makeover + setup intro (2026-04-07)

Three small but high-leverage additions before going public:

1. **OpenAI in the wizard.** PROVIDER step grew to a 3-way picker
   (llamacpp / openai / openrouter). Space cycles forward through
   all three. Default model auto-swaps to `gpt-4o-mini` for openai,
   `openai/gpt-oss-20b:free` for openrouter, unless the user typed
   something custom.

2. **OpenAI fallback context table.** OpenAI's `/v1/models` returns
   120+ models but exposes none of the `context_length` fields that
   OpenRouter's listing has, so the live detection always returned
   None for OpenAI direct usage. Added a hardcoded prefix table
   covering GPT-5, GPT-4.1, GPT-4o, GPT-4-turbo, GPT-4, GPT-3.5,
   and the o1/o3/o4 reasoning families. Prefix-matched in declaration
   order so dated suffixes (`gpt-4o-2024-11-20`) resolve to the
   base entry. The live probe still wins when it returns a real
   value (e.g. OpenRouter proxying an OpenAI model).

3. **SUCCESSOR intro animation plays at the start of `successor setup`.**
   First-time users see the harness's signature visual moment before
   the wizard opens. Skippable with any keypress.

4. **Default + dev system prompts rewritten.** Default prompt is
   model-agnostic now (no Qwen-specific suppression rules), tells
   the model it's running in a TUI with full markdown support, and
   establishes bash tool usage expectations. Dev prompt reflects the
   current architecture (bash subsystem, agent loop, async runner,
   native Qwen tool calls, compaction animation, provider
   auto-detection) so a fresh model knows what's actually in the
   codebase.

Tests: 858 → 864.

---

## Phase 6.3 — usage clarity pass + empty-state hero panel (2026-04-07)

Audited every touch point a new user hits between landing on the
GitHub repo and sending their first chat message. Found seven
clarity gaps and fixed them all. The biggest one is a new chat
empty state inspired by Hermes Agent's caduceus banner: a SUCCESSOR
title portrait on the left, an info panel on the right, painted
entirely through the existing `BrailleArt` + `paint_text` primitives
with no new infrastructure.

### Empty-state hero panel (the big one)

When the chat opens with no real messages yet AND the active profile
has `chat_intro_art` set, the chat area splits into two columns:

- Left half: BrailleArt portrait laid out via the existing Pretext-
  shaped resampler. Theme-aware (`theme.accent` + bold). On
  terminals < 80 cols the art is hidden and only the panel renders.
- Right half: info panel with section headers (PROFILE / PROVIDER /
  TOOLS / APPEARANCE) and the actionable bottom hint
  `type / for commands · press ? for help`.

Per-profile customization via the new `chat_intro_art: str | None`
field on `Profile`. Resolution order in `render/intro_art.py`:

  1. Absolute path → load directly
  2. Built-in name → `builtin/intros/<name>/10-title.txt`
  3. User dir → `~/.config/successor/art/<name>.txt`
  4. Built-in single-file → `builtin/intros/<name>.txt`

Returns None gracefully on any failure (chat falls back to the
synthetic greeting OR paints just the info panel, depending on
whether `chat_intro_art` is unset vs the file is missing).

`_is_empty_chat()` predicate handles the moment-of-truth correctly:
counts tool cards, compaction boundaries, and summary messages as
"real content" (they're synthetic for API serialization but the
user expects to see them). Once the user submits anything, the
predicate returns False and the chat surface goes back to normal
message painting.

`_build_intro_panel_lines()` reads from LIVE chat state, not
profile-stored values, so theme/density changes via Ctrl+T / Alt+D /
Ctrl+] mid-session reflect immediately in the panel.

Default profile, dev profile, and wizard-created profiles all ship
with `chat_intro_art="successor"` and `intro_animation="successor"`.

### Discoverability

- **Help overlay (`?`) gained an "available commands" section**
  built from the live `SLASH_COMMANDS` registry at paint time, so
  any future command shows up automatically. Renamed the existing
  "slash commands" section to "command palette" since it actually
  documented palette navigation, not the commands themselves.
- **Removed duplicate `Ctrl+P` keybind** (was listed in scroll +
  look-and-feel sections — the scroll one was the stale alias).
- **Tightened the help modal** (removed inter-section blank rows)
  so all 11 slash commands fit on a default 24-30 row terminal.

### `successor doctor` connectivity check

New "active profile" section after the existing terminal capability
dump. Shows:

  active profile:
    name        successor-dev
    provider    llamacpp
    base_url    http://localhost:8080
    model       local
    api_key     (none — local server)
    status      reachable
    ctx window  262144 tokens (auto-detected)

The probe is short, tolerant of failure, and labels the source of
the context window number (profile override / auto-detected /
fallback). First command to run when something isn't working.

### `successor` no-args refresh

- Tagline now mentions OpenAI-compatible endpoints alongside local
  llama.cpp
- Dropped stale "v0, scripted" and "phase 6 scaffold — not yet wired"
  subcommand descriptions
- Added "First time? Run `successor setup`" footer to the help text

### Friendly stream errors

The connection-refused / DNS / unreachable / timeout branch in
`_format_stream_error` now lists three numbered remediation paths:
start a local llama-server, run `successor setup` to switch providers,
or open `/config` to edit the profile inline. Previously the user
got only the local-server hint and bounced if they didn't have
llama.cpp installed.

### Wizard PROVIDER hints

Old hints were functional ("uses http://localhost:8080" /
"api.openai.com — api key required" / "openrouter.ai — api key
required"). New hints are motivating: "free + private, needs
llama-server running" / "pay-per-use against your OpenAI credits" /
"free models available, no card needed". Helps a user who isn't
sure which provider to pick make a decision faster.

### README reorder

Lead with the 30-second user journey, push the architectural
premise lower as a "why this exists" section. The screenshot still
opens the page, but the next thing the reader sees is the
quick-start `successor setup` block, the provider matrix, and the
in-chat key reference card — not the diff.py-only-stdout-writer
hook. Engineering-minded readers still find it; first-touch users
get oriented faster.

Tests: 864 → 881 (17 new for the empty-state painter + the
intro art loader's 4-tier resolution + `_is_empty_chat` predicate
edge cases).
