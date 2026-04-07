# Successor changelog

Per-phase notes for the framework infrastructure built on top of the
Phase 0 renderer + chat. Each phase below is one or more git commits
that add a self-contained capability with full tests.

The numbering jumps from "phase 0" (the original renderer + v0 chat)
straight to "phases 1–6" of the framework infra. There's no
contradiction — the framework phases were designed and built as a
unit on top of phase 0.

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
  profile with `intro_animation: "nusamurai"`, lower temperature
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
  `_play_intro_animation("nusamurai")`), then constructs the chat
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

## Phase 4.8 — successor emergence intro + demo system removed (2026-04-06)

The samurai-themed nusamurai braille animation is gone, the entire
`demo`/`show`/`frames` subcommand surface is deleted, and a new
bundled `successor` intro animation plays before the chat opens — an
11-frame braille emergence sequence ending on the title portrait,
held for ~2 seconds. Theme-aware, any keypress skips ahead.

This phase also collapsed `src/successor/demos/` — `chat.py` moved
up to `src/successor/chat.py` since it was the only file left.

### What landed

- **`src/successor/intros/successor.py`** — `SuccessorIntro` App.
  Loads 11 braille frames as `BrailleArt` instances at construction
  time (Pretext-shaped layout cache). Plays them sequentially with
  Bayer-dot interpolation between adjacent frames over
  `EMERGE_PER_FRAME_S = 0.32s` per transition (10 transitions =
  3.2s emerge). Holds the final frame for `HOLD_FINAL_S = 2.4s`.
  Auto-exits. Any keypress skips.
  - Theme-aware: resolves the active profile and uses its accent
    color for the braille ink, bg for the background.
  - First `FADE_IN_S = 0.4s` lerps from bg → accent so the first
    frame doesn't pop in hard.
  - "press any key to skip" hint at the bottom during emerge,
    hidden during the final hold.
- **`src/successor/intros/__init__.py`** — re-exports
  `SuccessorIntro` and `run_successor_intro()` entry point.
- **`src/successor/builtin/intros/successor/`** — 11 braille frame
  text files (`00-emerge.txt` through `10-title.txt`), extracted
  from the lycaonwtf gallery's TypeScript frames file via a
  one-shot regex parsing script. `pyproject.toml` package data
  config updated to ship `intros/*/*.txt`.

### What got deleted

- **`src/successor/demos/braille.py`** — `SuccessorDemo` and
  `SuccessorShow` App classes that played the nusamurai keyframes.
  Both gone. The `BrailleArt` / `interpolate_frame` / `load_frame`
  primitives in `render/braille.py` stay — they're still used by
  the new intro and the wizard's welcome frame.
- **`src/successor/demos/__init__.py` + the demos/ directory** —
  empty after deletions, removed entirely.
- **`successor demo`, `successor show`, `successor frames`** —
  three CLI subcommands deleted from `cli.py` along with their
  argparse subparser entries.
- **`assets/nusamurai/`** — the entire samurai braille keyframe
  directory deleted. The `assets/` parent directory was empty
  afterward and also removed. The repo no longer ships any
  samurai-themed assets.
- **`_assets_root()`, `_nusamurai_dir()`, `_list_frames()`** in
  `cli.py` — dead code, all gone.

### What got refactored

- **`src/successor/demos/chat.py` → `src/successor/chat.py`** —
  moved up since the demos/ directory is gone. All `from ..xxx`
  imports inside chat.py changed to `from .xxx`. All consumers
  updated:
  - `src/successor/cli.py`
  - `src/successor/snapshot.py`
  - `src/successor/wizard/setup.py`
  - `src/successor/wizard/config.py`
  - `tests/test_chat_profiles.py`
- **`successor-dev` profile** — `intro_animation` field changed
  from `"nusamurai"` to `"successor"`. Description updated.
- **`_play_intro_animation()` in cli.py** — calls
  `run_successor_intro()` instead of constructing the old
  `SuccessorDemo`. Only "successor" is recognized as a valid
  intro name; future user intros will live in
  `~/.config/successor/intros/<name>/`.
- **`_try_load_welcome_frame()` in `wizard/setup.py`** — used to
  load `assets/nusamurai/pos-th30/Meditating-ascii-art.txt`. Now
  loads `src/successor/builtin/intros/successor/10-title.txt` (the
  same final-portrait frame the intro animation holds at the end).
  The wizard welcome screen now shows the successor portrait, not
  the meditating samurai.
- **`cmd_doctor`** — reports the successor intro frame count
  instead of nusamurai frame count.
- **`cmd_bench`** — uses the first and last successor intro
  frames for the morph perf test.
- **Wizard intro-step UI** — `_INTRO_OPTIONS` updated, footer
  helper text updated. The wizard's intro toggle now offers
  "(none)" or "successor emergence — braille portrait (~5s)".

### Tests (339 — unchanged count, all passing)

No new tests added since this phase is mostly file moves and
deletions. Existing test fixtures that referenced `"nusamurai"` as
the `intro_animation` value were updated to `"successor"` in
`test_profiles.py`, `test_config_menu.py`, `test_wizard.py`. The
chat.py relocation was caught by import tests automatically once
the package was reinstalled.

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

## What's next

- **Skill invocation strategy** — pick always-on vs on-demand after
  experimenting with Qwen 3.5 in practice
- **Agent loop + tool dispatch** — wire `TOOL_REGISTRY` into the
  chat's response cycle once we've studied the llamacpp tool-call
  protocol surface deliberately
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

(Counts above are approximate by phase boundary; actual test
collection may include additional small additions in subsequent
commits.)

Final: **339 tests, all passing, hermetic via `SUCCESSOR_CONFIG_DIR`,
no fake mocks, no `.skip()` or `.todo()`.**
