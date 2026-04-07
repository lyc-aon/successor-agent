# Ronin changelog

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

- `src/ronin/loader.py` — generic `Registry[T]` with built-in dir +
  user dir loading, name-collision precedence (user wins), parser
  failure skipping with stderr warnings, hermetic-testable via
  `RONIN_CONFIG_DIR` env var
- `src/ronin/render/theme.py` rewritten — `ThemeVariant` (the 9
  semantic color slots, one per mode) + `Theme` bundle (name + icon +
  description + dark variant + light variant). `parse_color()` accepts
  hex strings AND oklch tuples/strings. `blend_variants()` lerps
  between variants for smooth transitions. `THEME_REGISTRY` singleton.
  `find_theme_or_fallback()` always returns a valid Theme.
- `src/ronin/builtin/themes/steel.json` — the default theme, ported
  from the previous DARK_THEME/LIGHT_THEME oklch values into one
  bundle with both variants
- `docs/example-themes/forge.json` — example user theme, hand-tuned
  warm samurai red palette with both dark and light variants. Drop
  into `~/.config/ronin/themes/` to install.
- `src/ronin/config.py` extended — added `version`, `display_mode`,
  `active_profile` slots; v1 → v2 migration translates legacy `theme`
  values (`dark` → `(steel, dark)`, `light` → `(steel, light)`,
  `forge` → `(forge, dark)`) idempotently
- `src/ronin/demos/chat.py` refactored to use ThemeVariant — every
  painter now takes `theme: ThemeVariant`, the chat resolves the
  current variant once per frame via `_current_variant()` which
  blends across both axes (theme transition + mode transition).
  Added `Alt+D` keybind for display mode toggle. Added `/mode`
  slash command. Added display mode widget (☾/☀ pill) to the title
  bar between density and theme. Made the `/theme` completer dynamic
  so user themes show up in autocomplete.
- `src/ronin/render/terminal.py` — file descriptors are now resolved
  lazily so the renderer is testable under pytest's captured stdin
  (which has no `fileno()`). The diff layer's stdout writes are
  unchanged in real terminal use.
- `src/ronin/snapshot.py` + `cli.py` — `chat_demo_snapshot()` accepts
  a `display_mode` parameter; `rn snapshot --display-mode` flag added.
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
  `RONIN_CONFIG_DIR` for hermetic isolation

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

- `src/ronin/providers/base.py` — `ChatProvider` Protocol (structural,
  runtime-checkable). Re-exports the `ChatStream` event types so
  callers can `from ronin.providers import StreamEnded` regardless of
  which backend is producing them.
- `src/ronin/providers/llama.py` — added `provider_type = "llamacpp"`
  class attribute. No behavioral changes.
- `src/ronin/providers/openai_compat.py` — `OpenAICompatClient` for
  any OpenAI-API-compatible server (LM Studio, Ollama, vLLM, OpenRouter,
  hosted servers). Optional `Authorization: Bearer` header via an
  `_AuthenticatedChatStream` subclass that injects the header before
  the urlopen call. `/v1/models` is the liveness probe (the OpenAI
  spec doesn't include `/health`).
- `src/ronin/providers/factory.py` — `make_provider(config)` reads
  the `type` field and dispatches to the matching constructor. Aliases
  supported (`llama`, `llama.cpp`, `openai`, `openai-compat`). Key
  translation (`max_tokens` → `default_max_tokens`). Forward-compat:
  unknown keys are silently dropped so future profiles don't break
  older Ronin installs.
- `src/ronin/providers/__init__.py` — re-exports for the public surface

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
profiles drop into `~/.config/ronin/profiles/*.json`.

### What landed

- `src/ronin/profiles/profile.py` — `Profile` dataclass, JSON parser
  with type-tolerant fallback (wrong-typed fields revert to dataclass
  defaults instead of crashing), `PROFILE_REGISTRY` singleton,
  `get_active_profile()` chain (chat.json → "default" → first
  registered → hardcoded fallback)
- `src/ronin/profiles/__init__.py` — public surface
- `src/ronin/builtin/profiles/default.json` — general-purpose profile,
  no intro animation
- `src/ronin/builtin/profiles/ronin-dev.json` — harness-development
  profile with `intro_animation: "nusamurai"`, lower temperature
  (0.5), 64K max_tokens, and a system prompt that primes the model
  for Ronin codebase work
- `src/ronin/demos/chat.py` — `RoninChat.__init__` accepts a
  `profile=` argument and resolves theme/mode/density/system_prompt/
  provider from it. Saved config still wins per-setting so manual
  changes persist. Added `_set_profile()`, `_cycle_profile()`,
  `/profile` slash command, `Ctrl+P` keybind, profile-name title bar
  widget, "profile" hit-box action for mouse mode, breadcrumb
  synthetic message on swap.
- `src/ronin/demos/braille.py` — `RoninDemo` gained `max_duration_s`
  (auto-exit after N seconds) and `intro_mode` (any keypress exits
  early) parameters
- `src/ronin/cli.py` — `cmd_chat` now resolves the active profile,
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
- The `ronin-dev` profile is what we use to dogfood the harness on
  itself. Activating it (`/profile ronin-dev` or set
  `active_profile: "ronin-dev"` in chat.json) loads the rendering
  rules into the system prompt.

---

## Phase 5 — Skill loader scaffolding (2026-04-06)

Loader-only. Skills are markdown files with YAML-style frontmatter
(Claude-Code-compatible format), loaded via the same Registry pattern
themes use. The chat doesn't yet send skill bodies to the model —
that decision (always-on prepend vs on-demand tool) waits for hands-on
time with Qwen 3.5.

### What landed

- `src/ronin/skills/skill.py` — `Skill` dataclass (name, description,
  body, source_path, `estimated_tokens` property using chars/4
  heuristic). `parse_skill_file()` reads `*.md` files, splits the
  frontmatter block via a tiny line-based parser (no PyYAML
  dependency), returns None for files without frontmatter so READMEs
  drop into `skills/` cleanly. `SKILL_REGISTRY` singleton.
- `src/ronin/skills/__init__.py` — public surface
- `src/ronin/builtin/skills/ronin-rendering-pattern.md` — the One Rule
  + five-layer architecture as a bundled skill. Self-documenting
  example of the format.
- `src/ronin/cli.py` — `rn skills` subcommand lists every loaded skill
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
  symlinks or copies it into `~/.config/ronin/skills/`. Same format,
  same loader contract.
- `_split_frontmatter` is intentionally minimal (no nested keys, no
  quoted values, no multiline values). Matches how Claude Code skills
  are written in practice. PyYAML can be added later if a power user
  wants nested fields.

---

## Phase 6 — Tool registry scaffolding (2026-04-06)

Loader-only. Tools are the only Ronin extension type that turns data
into code, so the registry has its own shape: a Python module
importer that harvests `@tool`-decorated functions instead of parsing
file contents. User tools are GATED behind a config flag (default
OFF) and audited to stderr when enabled.

### What landed

- `src/ronin/tools/tool.py` — `@tool(name=, description=, schema=)`
  decorator, `Tool` dataclass (callable via passthrough), `ToolRegistry`
  with module-import-based discovery. Built-in tools always load;
  user tools require `allow_user_tools: true` in chat.json. Each
  user tool import is announced to stderr. Partial-import rollback
  prevents a half-imported file from leaking partial registrations.
  Multi-tool files are supported (multiple @tool decorators per .py).
- `src/ronin/tools/__init__.py` — public surface
- `src/ronin/builtin/tools/__init__.py` — package marker
- `src/ronin/builtin/tools/read_file.py` — example built-in tool with
  full JSON schema, demonstrating the API
- `src/ronin/cli.py` — `rn tools` subcommand lists every registered
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
  ronin process. The gate exists so a misconfigured profile can't
  surprise the user with arbitrary code execution.

---

## What's next

- **Phase 4: `rn setup` wizard (the showcase)** — multi-region App
  with a live preview pane that uses `blend_variants` to smoothly
  morph through theme + mode options as the user arrows through them.
  Output is a profile JSON file. The single piece that proves the
  harness can build itself.
- **Skill invocation strategy** — pick always-on vs on-demand after
  experimenting with Qwen 3.5 in practice
- **Agent loop + tool dispatch** — wire `TOOL_REGISTRY` into the
  chat's response cycle once we've studied the llamacpp tool-call
  protocol surface deliberately
- **Framework docs** — once the surface is stable

---

## Test count by phase

| Phase | Tests added | Cumulative |
|---|---|---|
| 1 (loader + theme + config) | 88 | 88 |
| 2 (provider) | 19 | 107 |
| 3 (profiles + intro) | 33 | 140 |
| 5 (skill loader) | 17 | 157 |
| 6 (tool registry) | 15 | 187 (subset before this commit set may differ) |

Final: **187 tests, all passing, hermetic via `RONIN_CONFIG_DIR`,
no fake mocks, no `.skip()` or `.todo()`.**
