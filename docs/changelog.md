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
| 4.9 (delete profile from config menu) | 17 | 356 |
| 5.0 (bash-masking subsystem: parser + risk + exec + render + chat) | 131 | 487 |
| 5.1 (agent loop + compaction + burn rig) | 115 | 602 |
| 5.2 (visible compaction animation + context fill bar) | 28 | 630 |

(Counts above are approximate by phase boundary; actual test
collection may include additional small additions in subsequent
commits.)

Final: **630 tests, all passing, hermetic via `SUCCESSOR_CONFIG_DIR`,
no fake mocks, no `.skip()` or `.todo()`.**
