# Chat Render Refactor

Date: 2026-04-09
Status: Implemented and verified

## Purpose

The low-level renderer in Successor is not the main problem. The fragile
part is the chat scene composition that currently lives inside
`src/successor/chat.py`. This document maps the current implementation,
defines the invariants we cannot break, and gives a deterministic
behavior-preserving extraction plan plus a verification plan strong enough
to catch subtle render regressions.

This was a refactor plan, not a redesign brief. The first job was to make
the current render method easier to understand and safer to change while
preserving the renderer primitives and the existing composition model.

## Result

The chat scene composition has now been extracted out of
`src/successor/chat.py` into dedicated render modules without changing the
low-level renderer stack or terminal ownership model:

- `src/successor/render/chat_frame.py`
  - layout dataclasses and frame-partition helpers
- `src/successor/render/chat_header.py`
  - header composition and widget placement
- `src/successor/render/chat_viewport.py`
  - viewport sizing, scroll math, and body-width policy
- `src/successor/render/chat_intro.py`
  - intro hero + info-rail painting
- `src/successor/render/chat_overlays.py`
  - help modal and slash-command dropdown painting
- `src/successor/render/chat_input.py`
  - input, search, and footer-region painters
- `src/successor/render/chat_rows.py`
  - row-level message and card rendering helpers

`src/successor/chat.py` remains the controller/runtime owner. It still owns
input, stream/tool/subagent/compaction state, but now delegates frame
composition to explicit render seams instead of carrying all scene math
inline.

## Verification Completed

Automated verification that passed after extraction:

- `ruff check src/successor/chat.py src/successor/render/chat_*.py tests/test_chat_render_layout.py tests/test_chat_render_viewport.py`
- `PYTHONPATH=src python3 -m compileall src/successor`
- `PYTHONPATH=src pytest -q tests/test_snapshot_themes.py tests/test_intro_art.py tests/test_intro_sequence.py tests/test_chat_perf.py tests/test_chat_mouse.py tests/test_chat_paste.py tests/test_compaction_animation.py tests/test_playback.py tests/test_terminal.py tests/test_chat_render_layout.py tests/test_chat_render_viewport.py`
  - `129 passed`
- `PYTHONPATH=src pytest -q`
  - `1227 passed`

Manual and visual verification that passed:

- headless intro-frame verification through `SuccessorChat.on_tick()` +
  `RecordingBundle`
- headless help-overlay verification with real key input (`?`)
- headless slash-autocomplete verification with real key input (`/th`)
- headless human-emulated runtime session using real key input plus a live
  llama.cpp model:
  - `?` open/dismiss help
  - `/theme paper`
  - `/mode light`
  - `/density spacious`
  - `/budget`
  - `/bash printf 'render-check\\n'`
  - two live model turns producing formatted Markdown/tool output
- browser screenshots captured from generated playback viewers for intro,
  help, autocomplete, and runtime frames to verify final composition

The runtime verification bundle for this pass was produced under
`/tmp/render-refactor-verify/` during the refactor review.

## Notes

The seam map and checklist below are retained because they document the
baseline that the extraction followed. Some line references in the
"Current State" section describe the pre-extraction `chat.py` layout and
should be read as historical context rather than current file coordinates.

## Scope

In scope:

- Chat scene composition and layout inside `src/successor/chat.py`
- Extraction of pure helpers and scene modules from `chat.py`
- Snapshot, perf, visual, and human-emulated verification for the chat UI
- Documentation of current responsibilities and module boundaries

Out of scope for this pass:

- Rewriting the low-level renderer in `src/successor/render/*`
- Introducing Rich, prompt_toolkit Application, Textual, or another screen owner
- Visual redesign of the chat surface
- Reworking playback HTML or recorder UX
- Changing the theme model, density model, or terminal ownership contract

## Current State

### Stable renderer layers

The low-level renderer is already in good shape and should be treated as
stable infrastructure:

- `src/successor/render/cells.py`
  Defines `Style`, `Cell`, and `Grid`
- `src/successor/render/paint.py`
  Pure grid mutation helpers and box/text painting
- `src/successor/render/diff.py`
  The only stdout writer in the codebase
- `src/successor/render/app.py`
  Frame loop, buffering, input wakeups
- `src/successor/render/terminal.py`
  TTY ownership and restore logic
- `src/successor/snapshot.py`
  Headless render entrypoints used by tests and documentation

The foundational architecture described in `docs/rendering-plan.md` and
`docs/rendering-superpowers.md` still matches the low-level render stack.

### Current chat render seam map

As of 2026-04-09, the render-heavy part of `src/successor/chat.py` is
clustered in these symbols:

- `_Message` at line 1220
  View-facing message state plus caches
- `_RenderedRow` at line 1353
  Intermediate paint model for chat rows
- `SuccessorChat` at line 1753
  App/controller/runtime owner
- `on_tick()` at line 6938
  Per-frame orchestration, layout partitioning, header composition,
  region painting, recorder capture
- `_paint_chat_area()` at line 7159
  Chat viewport layout, body width policy, row slicing, scroll policy,
  streaming row merge
- `_paint_empty_state()` at line 7324
  Intro hero art + info rail layout and rendering
- `_build_message_lines()` at line 7982
  Compaction-aware row routing
- `_build_rows_from_messages()` directly below
  Main message-to-row flattening pass
- `_render_tool_card_rows()` at line 8272
  Static tool card sub-grid render and row snapshot
- `_render_running_tool_card_rows()` at line 8366
  Live tool card sub-grid render
- `_paint_autocomplete()` at line 8890
  Slash popover entrypoint
- `_paint_help_overlay()` at line 9148
  Centered modal layout and render
- `_paint_static_footer()` below the help overlay
  Context bar and compaction/warming badges
- `_paint_input()` at line 9448
  Input area, ghost text, cursor, overflow badge

### Why `chat.py` feels precarious

`chat.py` currently mixes five concerns in one file and often in one method:

- Runtime and event orchestration
- UI state ownership
- Frame geometry decisions
- Scene composition and painting
- Render-time state mutation

The main risk is not "the renderer is bad." The main risk is that the
chat scene code mutates controller state while it paints, so it is hard
to reason about what is a pure render decision versus what is app state.

### Render-time mutations currently happening

These are the main stateful seams to shrink or relocate:

- `on_tick()` resets `self._hit_boxes` and painters repopulate it
- `_paint_chat_area()` updates:
  - `self.scroll_offset`
  - `self._auto_scroll`
  - `self._last_chat_h`
  - `self._last_chat_w`
  - `self._last_total_height`
- Header/widget painting in `on_tick()` appends click targets directly
- Autocomplete painters append hitboxes during render
- Recorder capture happens at the end of `on_tick()`

Not all of these are wrong. The problem is that the contracts are mostly
implicit. The refactor should make these mutations explicit and narrow.

### Existing verification leverage

The project already has useful render verification infrastructure:

- `src/successor/snapshot.py`
  Headless chat/config/wizard frame rendering
- `tests/test_snapshot_themes.py`
  Scenario matrix smoke tests for chat rendering
- `tests/test_intro_art.py`
  Intro art rendering checks
- `tests/test_intro_sequence.py`
  Startup/intro behavior checks
- `tests/test_chat_perf.py`
  Render perf guardrails
- `tests/test_chat_mouse.py`
  Mouse interaction coverage
- `tests/test_chat_paste.py`
  Input wrapping and paste behavior
- `tests/test_compaction_animation.py`
  Animated compaction surface coverage
- `tests/test_playback.py`
  Session playback surfaces

This means the refactor should lean on stronger seam tests, not invent a
completely new test philosophy.

## Non-Negotiable Invariants

These rules are hard constraints for the refactor:

1. `src/successor/render/diff.py` remains the only stdout writer.
2. The low-level renderer stays in place. No framework swap.
3. This pass is behavior-preserving. No intentional UI redesign.
4. `snapshot.py` must continue to render the chat headlessly.
5. Recorder capture and playback bundles must remain valid.
6. Theme and display-mode behavior must stay identical.
7. Density behavior must stay identical.
8. Resize behavior must not get worse.
9. Search, help, autocomplete, input, tool cards, intro art, footer,
   compaction visuals, and streaming all remain functional.
10. Perf must remain within the current budget envelope.

## North Star

After the refactor:

- `chat.py` owns runtime and app state
- extracted modules own pure-ish scene description and painting
- geometry decisions are represented as explicit data, not scattered math
- render-time mutations are applied by the controller from named decisions
- a visual change requires touching one render module, not spelunking
  through the controller

The desired result is not "many tiny files." The desired result is one
coherent separation:

- controller logic in `chat.py`
- chat scene/layout/painters in `src/successor/render/`

## Target Module Shape

The safest extraction shape is incremental and close to current concepts.
The recommended target is:

- `src/successor/render/chat_frame.py`
  Dataclasses for rects, header widget placement, viewport layout,
  footer metadata, overlay geometry
- `src/successor/render/chat_header.py`
  Header composition and hitbox mapping
- `src/successor/render/chat_viewport.py`
  Chat body width policy, viewport slicing, scroll decisions
- `src/successor/render/chat_intro.py`
  Empty-state hero and info rail
- `src/successor/render/chat_rows.py`
  `_RenderedRow`-level row building helpers extracted from `chat.py`
- `src/successor/render/chat_overlays.py`
  Help modal and slash autocomplete
- `src/successor/render/chat_input.py`
  Input/search bar/footer painting helpers

`chat.py` should remain the owner of:

- event decoding
- stream/tool/subagent/compaction pumps
- theme resolution for the current frame
- chat state and mutations
- final orchestration of one frame
- recorder handoff

## Deterministic Extraction Checklist

Do these in order. Do not batch phases together.

### Phase 0: Freeze the baseline

- Record the current render seam map and module intent in docs
- Run the current automated render-related suite and save the command list
- Generate baseline snapshots for representative scenarios
- Capture at least one headed recording showing the live terminal surface
- Note any already-known visual quirks so they are not mistaken for new regressions

Acceptance gate:

- Baseline tests are green
- Baseline snapshots exist for comparison
- Baseline headed recording exists

### Phase 1: Extract frame geometry, no visual change

- Move title/input/footer/chat rect calculations out of `on_tick()`
- Introduce a single frame-layout dataclass in `render/chat_frame.py`
- Keep `on_tick()` behavior identical; only replace inline math with helpers
- Do not move painters yet

Acceptance gate:

- Snapshot text and ANSI match baseline for the standard matrix
- No hitbox coordinates change
- `test_chat_perf.py` stays green

### Phase 2: Extract header composition

- Move the title row widget placement into `render/chat_header.py`
- Header helper returns:
  - text placements
  - widget styles or widget metadata
  - hitboxes
- `chat.py` applies the returned placements

Acceptance gate:

- Header content matches baseline across width matrix
- Title clamping behavior is unchanged on narrow widths
- Mouse hit targets for theme/mode/density/profile/tasks/scroll remain correct

### Phase 3: Extract viewport layout and scroll policy

- Move body width, gutter, viewport slice, and visible-row range logic into
  `render/chat_viewport.py`
- Split calculation from mutation:
  - helper computes `ViewportDecision`
  - controller applies scroll/cache updates explicitly
- Keep row painting where it is until the viewport decision object is stable

Acceptance gate:

- Scrolling, scroll anchoring, and auto-scroll behavior remain unchanged
- Streaming rows only append while anchored at bottom
- Resize preserves the same visible region as before

### Phase 4: Extract empty-state intro surface

- Move `_paint_empty_state()` and closely related intro helpers into
  `render/chat_intro.py`
- Preserve hero/info-rail behavior exactly before attempting any cleanup

Acceptance gate:

- `blank` snapshots match baseline across width and height matrix
- Intro art still hides gracefully on narrow or short terminals
- Theme-aware hero styling remains correct in light and dark

### Phase 5: Extract row building

- Move `_build_message_lines()`, `_build_rows_from_messages()`, and
  closely related helpers into `render/chat_rows.py`
- Keep `_Message` ownership in `chat.py` at first if that lowers risk
- Do not redesign the row model during extraction

Acceptance gate:

- Search highlighting, summary rows, boundaries, markdown rows, tool cards,
  and streaming integration all match baseline behavior
- Compaction animation phases remain correct

### Phase 6: Extract overlays and input surfaces

- Move autocomplete, help overlay, and input/footer painters into:
  - `render/chat_overlays.py`
  - `render/chat_input.py`
- Keep state derivation in `chat.py`; extracted modules should receive
  already-resolved state objects

Acceptance gate:

- Help modal geometry remains stable
- Slash command popovers still fit within the available space
- Input cursor, ghost text, overflow badge, and search bar behavior stay intact

### Phase 7: Normalize hitbox ownership

- Replace scattered `_hit_boxes.append(...)` with one explicit composition path
- Preferred pattern:
  - render helper returns `list[_HitBox]`
  - controller concatenates them and sets `self._hit_boxes` once per frame

Acceptance gate:

- Mouse behavior matches baseline
- No hitbox coverage holes appear
- Hitbox list ordering stays deterministic

### Phase 8: Final cleanup pass

- Reduce `on_tick()` to orchestration and region calls
- Remove dead locals and duplicate layout math
- Ensure `chat.py` only mutates app state in clearly named sections
- Update docs to reflect the new module boundaries

Acceptance gate:

- `chat.py` render path reads as controller logic, not scene implementation
- The render-related symbols moved out of `chat.py` match the documented target shape

## What We Should Not Do

These are explicit no-go moves for this refactor:

- No big-bang rewrite of the chat renderer
- No simultaneous redesign of the UI
- No theme-model rewrite
- No replacement of the cell-grid renderer
- No swapping snapshot tests for looser smoke tests
- No mixing playback-viewer work into this pass
- No "clean up while we're here" changes outside the chat render surface

## Automated Verification Plan

The refactor is not done when the code looks cleaner. It is done when
the render contract is proven intact.

### Existing suites that must stay green

Minimum automated gate:

- `tests/test_snapshot_themes.py`
- `tests/test_intro_art.py`
- `tests/test_intro_sequence.py`
- `tests/test_chat_perf.py`
- `tests/test_chat_mouse.py`
- `tests/test_chat_paste.py`
- `tests/test_compaction_animation.py`
- `tests/test_playback.py`
- `tests/test_terminal.py`

Then run the full suite once the seam tests pass.

### New seam tests to add during the refactor

Add small contract tests as modules are extracted:

- `tests/test_chat_render_layout.py`
  Frame rects, title clamp, width policy
- `tests/test_chat_render_viewport.py`
  Scroll anchoring, visible slice, bottom anchoring
- `tests/test_chat_render_hitboxes.py`
  Header/widget/overlay hitbox geometry
- `tests/test_chat_render_intro.py`
  Empty-state layout thresholds
- `tests/test_chat_render_overlays.py`
  Help and slash popover geometry/collision behavior

These tests should prefer exact geometry and deterministic outputs over
broad "contains text" assertions where feasible.

### Snapshot matrix

For each phase, run snapshot comparisons on at least:

- Scenarios:
  - `blank`
  - `showcase`
  - `thinking`
  - `search`
  - `help`
  - `autocomplete`
  - `tool_card`
- Themes:
  - `steel`
  - `paper`
- Display modes:
  - `dark`
  - `light`
- Densities:
  - `compact`
  - `normal`
  - `spacious`
- Sizes:
  - `80x24`
  - `100x30`
  - `120x30`
  - `160x40`

Expected rule:

- If the phase is behavior-preserving, the grid text and ANSI should
  match baseline exactly for the covered scenarios.
- Any intentional diff must be called out explicitly before proceeding.

### Perf gate

Run `tests/test_chat_perf.py` after every extraction phase that touches:

- row building
- viewport slicing
- intro art
- overlay painting

If perf regresses materially, stop and fix before proceeding.

## Human-Emulated Verification Plan

This refactor must also survive real use, not just snapshots.

### Manual session matrix

Run these in a headed terminal session after the final extraction pass:

1. Fresh launch at wide size
   Verify intro hero, info panel, title row, and footer
2. Fresh launch at narrow size
   Verify graceful collapse and title clamp
3. Drag-resize test
   Resize wide to narrow to wide while idle and while streaming
4. Multi-turn conversation
   Include markdown, code blocks, bullets, and long wrapped text
5. Tool execution
   Verify running tool card and completed tool card
6. Search mode
   Verify highlighted results and search-bar replacement
7. Slash autocomplete
   Verify command list, arg mode, and no-match mode
8. Help overlay
   Verify open/close and centered geometry
9. Compaction
   Verify waiting, materialize, reveal, and settled states
10. Recorder capture
   Verify the captured bundle still replays correctly
11. Mouse pass
   Verify top-row pills and autocomplete rows click correctly

### Visual verification checklist

For each manual run, inspect for:

- overlap between header controls and title
- clipped intro art or panel text
- body column drift across densities
- footer truncation or bad alignment
- popovers leaking underlying chat content
- cursor or ghost text painting errors
- scroll jumps after new content arrives
- flicker during resize
- theme/display-mode mismatch
- missing or offset hit targets

### Recorder and playback verification

Because the recorder captures the post-paint grid from `on_tick()`, the
refactor must verify both live rendering and captured rendering:

- record a live session after the refactor
- inspect `session_trace.json`
- open `playback.html`
- confirm the terminal frames reflect the live session accurately

## Done Definition

The refactor is successful only if all of the following are true:

- The new module boundaries match this plan or a documented approved revision
- `chat.py` is smaller and more obviously controller-oriented
- snapshot output is unchanged unless a diff was explicitly accepted
- render-related test suites are green
- perf is not materially worse
- headed human-emulated testing passes
- recorder playback still reflects the live terminal correctly
- docs describe the current render architecture accurately

## Rollback Triggers

Stop and revert the current phase if any of the following occurs:

- unexpected snapshot diffs across the standard matrix
- hitbox regressions
- resize flicker or visible tearing
- scroll anchoring changes without deliberate design
- intro art or tool cards rendering differently without intent
- recorder playback diverges from live render output
- perf regression that survives one focused optimization attempt

## Practical Execution Order

If we execute this plan soon, the safest order is:

1. Add the plan and seam tests
2. Extract frame geometry
3. Extract header
4. Extract viewport decision logic
5. Extract intro painter
6. Extract overlays and input/footer
7. Extract row-building helpers
8. Normalize hitbox composition
9. Run full automated and headed verification

This order keeps the highest-risk logic changes late, after the easier
scene boundaries are already explicit and covered.
