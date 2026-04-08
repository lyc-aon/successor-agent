"""SuccessorConfig — three-pane profile config menu with live preview.

Different shape from the wizard. Where the wizard is "create from
scratch, linear, one-shot," the config menu is "browse all your
profiles, edit anything, dirty-track, save/revert, see-it-live."

Layout (resize-aware, three panes + footer):

  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                              successor · config                                  │  title
  ├─────────────────────┬─────────────────────────────────┬──────────────────────┤
  │ profiles            │ settings                        │ preview              │
  │ ─────────           │ ───────                         │ ───────              │
  │ ▸ ◆ default      ●  │ appearance                      │ ┌──────────────────┐ │
  │   ▲ successor-dev    *  │     theme       steel  *        │ │ successor · chat …   │ │
  │                     │     mode        dark            │ │                  │ │
  │                     │     density     normal          │ │ successor            │ │
  │                     │                                 │ │ Greetings, …     │ │
  │                     │ behavior                        │ │                  │ │
  │                     │     intro       (none)          │ │ you              │ │
  │                     │     prompt      [edit JSON]     │ │ what's your …    │ │
  │                     │                                 │ │                  │ │
  │                     │ provider                        │ │ successor            │ │
  │                     │     type        llamacpp        │ │ Patience and …   │ │
  │                     │     model       local           │ │                  │ │
  │                     │     base_url    localhost…      │ │ ctx 48/256k loc… │ │
  │                     │     temperature 0.7             │ └──────────────────┘ │
  │                     │     max_tokens  32768           │                      │
  │                     │                                 │                      │
  │                     │ extensions                      │                      │
  │                     │     skills      (0)             │                      │
  │                     │     tools       (0)             │                      │
  ├─────────────────────┴─────────────────────────────────┴──────────────────────┤
  │ tab focus · ↑↓ navigate · ⏎ edit · s save · r revert · esc back to chat      │
  └──────────────────────────────────────────────────────────────────────────────┘

Renderer features (concepts.md categories):

  Cat 1 (mutable cells)        — settings rows re-style as the user
                                 edits them; the dirty `*` marker
                                 appears the moment a value changes
  Cat 2 (smooth animation)     — focused-pane border breathing pulse;
                                 save flash pulses every saved field
                                 green for 400ms; toast slide-in on
                                 save/revert
  Cat 3 (multi-region UI)      — three panes side by side with Tab to
                                 cycle focus; the focused pane gets a
                                 brighter border
  Cat 5 (deterministic)        — every focus state and dirty state is
                                 a snapshot fixture
  Cat 7 (programmatic UI)      — selecting a different profile in the
                                 left pane animates the preview chat
                                 through that profile's theme/mode/
                                 density via the existing blend machinery

The save flow writes one JSON file per dirty profile, then reloads
PROFILE_REGISTRY so the registry shows the new state. If the user
saved changes to the currently-active profile's appearance fields,
those fields are also updated in chat.json so the changes take effect
when the user returns to the chat (without needing to clear their
saved overrides manually).
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import load_chat_config, save_chat_config
from ..graphemes import (
    delete_next_grapheme,
    delete_prev_grapheme,
    next_grapheme_boundary,
    prev_grapheme_boundary,
)
from ..input.keys import (
    Key,
    KeyDecoder,
    KeyEvent,
)
from ..loader import config_dir
from ..profiles import (
    PROFILE_REGISTRY,
    Profile,
    all_profiles,
    get_active_profile,
)
from ..skills import SKILL_REGISTRY, all_skills, get_skill, recommended_skills_for_tools
from ..render.app import App
from ..render.braille import BrailleArt
from ..render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    ATTR_ITALIC,
    ATTR_REVERSE,
    Cell,
    Grid,
    Style,
)
from ..render.paint import (
    BOX_ROUND,
    fill_region,
    paint_box,
    paint_text,
)
from ..render.terminal import Terminal
from ..render.text import ease_out_cubic, lerp_rgb
from ..render.theme import (
    THEME_REGISTRY,
    Theme,
    ThemeVariant,
    all_themes,
    blend_variants,
    find_theme_or_fallback,
    normalize_display_mode,
    toggle_display_mode,
)
from ..subagents.config import SUBAGENT_STRATEGIES
from ..tools_registry import AVAILABLE_TOOLS, selectable_tool_names
from ..web.config import HOLO_DEFAULT_PROVIDER_OPTIONS
from .prompt_editor import PromptEditor


# ─── Constants ───

# Pane widths (computed dynamically from grid.cols, but with mins)
MIN_LEFT_W = 22
MIN_MIDDLE_W = 36
MIN_RIGHT_W = 32

# Focus border breathing rate — slower than the wizard's step pulse
# so it reads as ambient rather than attention-getting
BORDER_PULSE_HZ = 0.5

# Save flash duration (per-field) — how long the green pulse lasts
SAVE_FLASH_S = 0.6

# Toast lifetime
TOAST_DURATION_S = 2.5

# Section reveal animation when navigating between sections
SECTION_REVEAL_S = 0.18

# Inline edit overlay fade duration
EDIT_OVERLAY_FADE_S = 0.15


# ─── Setting field metadata ───


class FieldKind(Enum):
    """How a setting value is edited."""
    CYCLE = "cycle"            # ↑↓ through a fixed list (theme, density)
    TOGGLE = "toggle"          # binary flip on Enter (mode, intro)
    TEXT = "text"              # inline single-line text input
    NUMBER = "number"          # inline text input with int/float validation
    SECRET = "secret"          # inline text input, displayed as ••• when not editing
    MULTILINE = "multiline"    # full-screen text editor overlay
    TOOLS_TOGGLE = "tools"     # multi-select overlay of enabled tools
    READONLY = "readonly"      # hint-only, can't be edited from here


@dataclass(frozen=True, slots=True)
class _SettingField:
    """One row in the settings pane.

    section_label is used to group fields under headers — empty section
    labels mean "use the previous row's section."
    """
    name: str
    label: str
    section: str
    kind: FieldKind
    # For CYCLE fields: a list-getter that returns the available options.
    # For TOGGLE fields: a list of two values.
    # For READONLY/TEXT/NUMBER/SECRET/MULTILINE fields: empty.
    options_getter: Any = None  # Callable[[], list[str]] | None
    # For READONLY fields: a hint shown after the value
    hint: str = ""
    # For NUMBER fields: "int" or "float" to determine parser + display
    number_kind: str = "int"


# Settings tree definition. Order matters for navigation.
def _theme_options() -> list[str]:
    return sorted(t.name for t in all_themes())


def _provider_type_options() -> list[str]:
    """Cycle options for the provider_type field — pulled from
    PROVIDER_REGISTRY so adding a new backend automatically extends
    the cycle list."""
    from ..providers import PROVIDER_REGISTRY
    # Use only the canonical names (filter out aliases like "llama"
    # that point at the same constructor)
    canonical = sorted({
        cls.provider_type
        for cls in PROVIDER_REGISTRY.values()
        if hasattr(cls, "provider_type")
    })
    return canonical or ["llamacpp"]


_SETTINGS_TREE: tuple[_SettingField, ...] = (
    _SettingField(
        name="theme", label="theme", section="appearance",
        kind=FieldKind.CYCLE, options_getter=_theme_options,
    ),
    _SettingField(
        name="display_mode", label="mode", section="",
        kind=FieldKind.TOGGLE, options_getter=lambda: ["dark", "light"],
    ),
    _SettingField(
        name="density", label="density", section="",
        kind=FieldKind.CYCLE,
        options_getter=lambda: ["compact", "normal", "spacious"],
    ),
    _SettingField(
        name="intro_animation", label="intro", section="behavior",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [None, "successor"],
    ),
    _SettingField(
        name="system_prompt", label="prompt", section="",
        kind=FieldKind.MULTILINE,
    ),
    _SettingField(
        name="provider_type", label="type", section="provider",
        kind=FieldKind.CYCLE, options_getter=_provider_type_options,
    ),
    _SettingField(
        name="provider_model", label="model", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="provider_base_url", label="base_url", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="provider_api_key", label="api_key", section="",
        kind=FieldKind.SECRET,
    ),
    _SettingField(
        name="provider_temperature", label="temperature", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="provider_max_tokens", label="max_tokens", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    _SettingField(
        name="skills", label="skills", section="extensions",
        kind=FieldKind.TOOLS_TOGGLE,
    ),
    _SettingField(
        name="tools", label="tools", section="",
        kind=FieldKind.TOOLS_TOGGLE,
    ),
    # ── Bash tool flags ─────────────────────────────────────────────
    # These are per-tool knobs stored under
    # profile.tool_config["bash"]. They only render when bash is in
    # profile.tools — otherwise the rows hide themselves at paint
    # time (see `_bash_rows_visible`).
    _SettingField(
        name="bash_allow_dangerous", label="yolo mode", section="bash safety",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [False, True],
    ),
    _SettingField(
        name="bash_allow_mutating", label="allow mutating", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="bash_timeout_s", label="timeout (s)", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="bash_max_output_bytes", label="max output bytes", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    # ── Holonet API routes ──────────────────────────────────────────
    _SettingField(
        name="holonet_default_provider", label="default provider", section="holonet",
        kind=FieldKind.CYCLE,
        options_getter=lambda: list(HOLO_DEFAULT_PROVIDER_OPTIONS),
    ),
    _SettingField(
        name="holonet_brave_enabled", label="brave enabled", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="holonet_brave_api_key", label="brave api key", section="",
        kind=FieldKind.SECRET,
    ),
    _SettingField(
        name="holonet_brave_api_key_file", label="brave key file", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="holonet_firecrawl_enabled", label="firecrawl enabled", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="holonet_firecrawl_api_key", label="firecrawl api key", section="",
        kind=FieldKind.SECRET,
    ),
    _SettingField(
        name="holonet_firecrawl_api_key_file", label="firecrawl key file", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="holonet_europe_pmc_enabled", label="europe pmc", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="holonet_clinicaltrials_enabled", label="clinicaltrials", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="holonet_biomedical_enabled", label="biomedical combo", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    # ── Browser / Playwright ────────────────────────────────────────
    _SettingField(
        name="browser_headless", label="headless", section="browser",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="browser_channel", label="channel", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="browser_python_executable", label="python runtime", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="browser_executable_path", label="executable", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="browser_user_data_dir", label="user data dir", section="",
        kind=FieldKind.TEXT,
    ),
    _SettingField(
        name="browser_viewport_width", label="viewport width", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    _SettingField(
        name="browser_viewport_height", label="viewport height", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    _SettingField(
        name="browser_timeout_s", label="timeout (s)", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="browser_screenshot_on_error", label="shot on error", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    # ── Compaction (autocompactor thresholds + behavior) ────────────
    # All percentages are fractions of the resolved context window.
    # The chat builds the runtime ContextBudget by multiplying each
    # pct by the actual window size, with the corresponding floor as
    # a hard minimum so tiny windows still get usable headroom.
    _SettingField(
        name="compaction_enabled", label="enabled", section="compaction",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="compaction_warning_pct", label="warning %", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="compaction_autocompact_pct", label="autocompact %", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="compaction_blocking_pct", label="blocking %", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="compaction_keep_recent_rounds", label="keep recent rounds", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    _SettingField(
        name="compaction_summary_max_tokens", label="summary max tokens", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    # ── Background subagents (/fork + model-visible subagent tool) ─
    _SettingField(
        name="subagents_enabled", label="enabled", section="subagents",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
    _SettingField(
        name="subagents_strategy", label="scheduling", section="",
        kind=FieldKind.CYCLE,
        options_getter=lambda: list(SUBAGENT_STRATEGIES),
    ),
    _SettingField(
        name="subagents_max_model_tasks", label="max model tasks", section="",
        kind=FieldKind.NUMBER, number_kind="int",
    ),
    _SettingField(
        name="subagents_timeout_s", label="timeout (s)", section="",
        kind=FieldKind.NUMBER, number_kind="float",
    ),
    _SettingField(
        name="subagents_notify_on_finish", label="notify on finish", section="",
        kind=FieldKind.TOGGLE,
        options_getter=lambda: [True, False],
    ),
)

# Indices into _SETTINGS_TREE that ARE editable (used for cursor jumps)
_EDITABLE_INDICES = tuple(
    i for i, f in enumerate(_SETTINGS_TREE) if f.kind != FieldKind.READONLY
)


def _field_visible_for_profile(profile: Profile, field: _SettingField) -> bool:
    """Return True if this field should be shown for the given profile.

    Some fields are conditional on what else the profile has enabled.
    Right now the only conditional group is the bash_* fields: they
    only make sense when bash is in profile.tools, otherwise they're
    just noise. Adding a new tool with its own per-tool knobs later
    will extend this function — no other paint code needs to change.
    """
    if field.name.startswith("bash_"):
        return "bash" in (profile.tools or ())
    if field.name.startswith("holonet_"):
        return "holonet" in (profile.tools or ())
    if field.name.startswith("browser_"):
        return "browser" in (profile.tools or ())
    return True


def _visible_field_indices(profile: Profile) -> tuple[int, ...]:
    """All field indices visible for this profile, in tree order."""
    return tuple(
        i for i, f in enumerate(_SETTINGS_TREE)
        if _field_visible_for_profile(profile, f)
    )


# ─── Focus state ───


class Focus(Enum):
    PROFILES = "profiles"
    SETTINGS = "settings"


# ─── Toast / save flash ───


@dataclass
class _Toast:
    text: str
    started_at: float
    kind: str = "ok"  # "ok" | "warn" | "info"


@dataclass
class _SaveFlash:
    """Per-field green pulse that fades over SAVE_FLASH_S after a save."""
    field_keys: set[tuple[str, str]]  # (profile_name, field_name)
    started_at: float


# ─── Inline text editor state (TEXT / NUMBER / SECRET fields) ───


@dataclass
class _InlineTextEdit:
    """In-progress single-line text edit for a TEXT/NUMBER/SECRET field.

    Held by the config menu while the user is typing into a field row.
    Esc cancels (restores `snapshot`); Enter commits and parses if
    needed (NUMBER fields validate the buffer parses to int/float
    according to the field's number_kind).
    """
    field_idx: int
    buffer: str
    cursor: int  # byte offset into buffer
    snapshot: Any  # original value at edit-open time, for cancel
    kind: FieldKind  # mirror the field kind so the handler doesn't have to look it up


@dataclass
class _ToolsEdit:
    """In-progress tools multi-select edit.

    When the user presses Enter on the tools row, an overlay opens
    that lists every tool in AVAILABLE_TOOLS with a checkbox. The
    user arrows through the list; space toggles each; Enter commits
    the working selection; Esc restores the snapshot.

    The edit is LIVE — toggling a tool updates the profile immediately
    so the dirty marker + preview refresh as the user works. Esc
    undoes every change back to `snapshot`.
    """
    field_idx: int
    cursor: int
    snapshot: tuple[str, ...]  # original value for cancel


# ─── Delete confirmation modal state ───


@dataclass
class _DeleteConfirm:
    """In-flight 'delete profile?' confirmation modal.

    Two flavors based on `mode`:
      "delete"  — pure user profile, the JSON file gets unlinked
                  from disk and the row vanishes from the registry
      "revert"  — user override of a built-in, the user file gets
                  unlinked but the built-in re-emerges in its place
    """
    profile_idx: int  # index into _working_profiles
    profile_name: str
    mode: str  # "delete" | "revert"
    started_at: float  # for the fade-in animation


# ─── The config App ───


class SuccessorConfig(App):
    """Three-pane profile config menu.

    Constructed via `run_config_menu()` which handles the post-exit
    return into the chat App with the (possibly edited) active profile.
    """

    def __init__(
        self,
        *,
        terminal: Terminal | None = None,
    ) -> None:
        super().__init__(
            target_fps=30.0,
            quit_keys=b"\x03",  # only Ctrl+C — letters must be typeable
            terminal=terminal if terminal is not None else Terminal(bracketed_paste=True),
        )

        # Force fresh registry loads so user-installed themes/profiles
        # show up if they were added since startup
        THEME_REGISTRY.reload()
        PROFILE_REGISTRY.reload()
        SKILL_REGISTRY.reload()

        # ─── Profile state ───
        # Snapshot the registry at config-open time. _initial holds the
        # original values for revert; _working holds the (possibly
        # edited) values being displayed.
        self._initial_profiles: list[Profile] = [
            deepcopy(p) for p in all_profiles()
        ]
        self._working_profiles: list[Profile] = [
            deepcopy(p) for p in all_profiles()
        ]
        if not self._working_profiles:
            # Pathological — no profiles loaded. Stub one so the menu
            # has something to display instead of crashing.
            self._working_profiles = [Profile(name="(no profiles loaded)")]
            self._initial_profiles = list(self._working_profiles)

        # Active profile cursor in the left pane. Defaults to whichever
        # profile is currently active per chat.json.
        active = get_active_profile()
        self._active_idx = 0
        for i, p in enumerate(self._working_profiles):
            if p.name == active.name:
                self._active_idx = i
                break

        # Cursor positions in each pane
        self._profile_cursor: int = self._active_idx
        self._settings_cursor: int = 0
        # Force the cursor onto an editable row at startup
        self._settings_cursor = self._first_editable_at_or_after(0)

        # ─── Focus ───
        self._focus: Focus = Focus.SETTINGS

        # ─── Inline edit state ───
        # When the user presses Enter on a CYCLE field, an inline list
        # opens with the current value highlighted. The user navigates
        # ↑↓ to pick, Enter to confirm, Esc to cancel.
        self._editing_field: int | None = None  # index into _SETTINGS_TREE
        self._editing_cursor: int = 0
        # Snapshot of the value at edit-open time, for cancel
        self._edit_snapshot: Any = None

        # ─── Inline text edit state (TEXT / NUMBER / SECRET fields) ───
        self._inline_text_edit: _InlineTextEdit | None = None

        # ─── Tools multi-select edit state ───
        self._tools_edit: _ToolsEdit | None = None

        # ─── Multi-line prompt editor (MULTILINE fields) ───
        self._prompt_editor: PromptEditor | None = None

        # ─── Delete profile confirmation modal ───
        self._delete_confirm: _DeleteConfirm | None = None

        # ─── Dirty tracking ───
        # Set of (profile_name, field_name) tuples that differ from
        # the initial snapshot.
        self._dirty: set[tuple[str, str]] = set()

        # ─── Animation state ───
        self._toast: _Toast | None = None
        self._save_flash: _SaveFlash | None = None
        self._section_reveal_at: float = 0.0
        # Time the user last navigated between profiles, used for the
        # preview transition
        self._last_profile_switch_at: float = 0.0

        # ─── Live preview chat ───
        self._preview_chat = self._build_preview_chat()
        self._sync_preview()

        # ─── Exit signal ───
        # Set to True when the user wants the chat to reopen on a
        # specific profile (the one currently selected in the left
        # pane). The cli.py main loop checks this after run() returns.
        self.exit_to_chat: bool = False
        self.requested_active_profile: str | None = None

        # ─── Input parsing ───
        self._key_decoder = KeyDecoder()

    # ─── Preview chat construction ───

    def _build_preview_chat(self):
        """Construct the live preview chat — same pattern as the wizard."""
        from ..chat import SuccessorChat, _Message

        chat = SuccessorChat()
        chat.messages = []
        chat.messages.append(
            _Message(
                "successor",
                "Greetings, traveler. I am successor — the blade rests, "
                "the fire is warm. Speak.",
                synthetic=True,
            )
        )
        chat.messages.append(_Message("user", "what's your blade?"))
        chat.messages.append(
            _Message(
                "successor",
                "Patience and intent. Steel without those is just metal. "
                "The blade is the silence between heartbeats.",
                synthetic=True,
            )
        )
        return chat

    def _sync_preview(self) -> None:
        """Apply the cursor-selected profile to the preview chat.

        Uses the chat's existing _set_theme/_set_display_mode/
        _set_density methods so the smooth blend transitions run
        for free — no animation code in the config menu.
        """
        if not self._working_profiles:
            return
        profile = self._working_profiles[self._profile_cursor]

        target_theme = find_theme_or_fallback(profile.theme)
        if target_theme.name != self._preview_chat.theme.name:
            self._preview_chat._set_theme(target_theme)

        target_mode = normalize_display_mode(profile.display_mode)
        if target_mode != self._preview_chat.display_mode:
            self._preview_chat._set_display_mode(target_mode)

        from ..chat import find_density, NORMAL
        target_density = find_density(profile.density) or NORMAL
        if target_density.name != self._preview_chat.density.name:
            self._preview_chat._set_density(target_density)

    # ─── Editing helpers ───

    def _current_profile(self) -> Profile:
        """The profile under the LEFT pane cursor (the one being viewed)."""
        return self._working_profiles[self._profile_cursor]

    def _current_field(self) -> _SettingField:
        idx = max(0, min(self._settings_cursor, len(_SETTINGS_TREE) - 1))
        return _SETTINGS_TREE[idx]

    def _editable_visible_indices(self) -> tuple[int, ...]:
        """Editable field indices visible for the current profile.

        Dynamic because some fields (bash_* flags) only show when
        bash is in the profile's tools list. When the user toggles
        bash on/off the set of navigable rows changes with it.
        """
        profile = self._current_profile()
        return tuple(
            i for i in _EDITABLE_INDICES
            if _field_visible_for_profile(profile, _SETTINGS_TREE[i])
        )

    def _multi_select_names(self, field: _SettingField) -> tuple[str, ...]:
        """Return the selectable option names for tools/skills overlays."""
        if field.name == "tools":
            return selectable_tool_names()
        if field.name == "skills":
            return tuple(skill.name for skill in all_skills())
        return ()

    def _multi_select_label_desc(
        self,
        field: _SettingField,
        name: str,
    ) -> tuple[str, str]:
        """Display label + secondary description for a multi-select row."""
        if field.name == "tools":
            descriptor = AVAILABLE_TOOLS[name]
            return descriptor.label, descriptor.description
        skill = get_skill(name)
        if skill is None:
            return name, ""
        desc = skill.description or skill.when_to_use
        if skill.allowed_tools:
            tools = ", ".join(skill.allowed_tools)
            desc = f"{desc} [tools: {tools}]" if desc else f"tools: {tools}"
        return skill.name, desc

    def _first_editable_at_or_after(self, start_idx: int) -> int:
        """Find the first editable row index at or after start_idx."""
        editable = self._editable_visible_indices() or _EDITABLE_INDICES
        for i in editable:
            if i >= start_idx:
                return i
        # Wrap
        return editable[0] if editable else 0

    def _settings_move(self, delta: int) -> None:
        """Move the settings cursor by delta, skipping read-only rows."""
        editable = self._editable_visible_indices()
        if not editable:
            return
        current = self._settings_cursor
        # If the cursor is on a now-hidden row, snap it to the
        # nearest visible one before moving
        if current not in editable:
            self._settings_cursor = editable[0]
            current = editable[0]
        try:
            cur_pos = editable.index(current)
        except ValueError:
            cur_pos = 0
        new_pos = (cur_pos + delta) % len(editable)
        self._settings_cursor = editable[new_pos]
        self._section_reveal_at = self.elapsed

    def _profile_value_for_field(self, profile: Profile, field: _SettingField) -> Any:
        """Read the current value of a settings field for DISPLAY.

        Truncates long values, masks SECRET fields, formats numbers.
        Use `_profile_value_for_field_raw` for the raw value used in
        dirty comparison and editing.
        """
        if field.name == "system_prompt":
            # Show a truncated preview of the prompt for the row label
            return (profile.system_prompt[:24] + "…") if len(profile.system_prompt) > 24 else profile.system_prompt
        if field.name == "skills":
            if not profile.skills:
                return "(none)"
            if len(profile.skills) == 1:
                return profile.skills[0]
            return f"({len(profile.skills)})"
        if field.name == "tools":
            if not profile.tools:
                return "(none — chat only)"
            return ", ".join(profile.tools)
        if field.name.startswith("bash_"):
            raw = self._profile_value_for_field_raw(profile, field)
            if field.name == "bash_allow_dangerous":
                return "⚠ YOLO" if raw else "off (safe)"
            if field.name == "bash_allow_mutating":
                return "on" if raw else "read-only"
            if raw is None:
                return "(default)"
            return str(raw)

        if field.name.startswith("compaction_"):
            raw = self._profile_value_for_field_raw(profile, field)
            if field.name == "compaction_enabled":
                return "on (autocompact)" if raw else "off (manual only)"
            if field.name.endswith("_pct"):
                # Display as a percentage with 2 decimal places.
                # The user enters and edits as a percent (e.g. 6.25
                # for 6.25%) but the underlying value is a fraction.
                if isinstance(raw, (int, float)):
                    return f"{raw * 100:.2f}%"
                return "(default)"
            if isinstance(raw, (int, float)):
                return str(raw)
            return "(default)"

        if field.name.startswith("holonet_"):
            raw = self._profile_value_for_field_raw(profile, field)
            if field.name.endswith("_enabled"):
                return "on" if raw else "off"
            if field.kind == FieldKind.SECRET:
                if raw is None or raw == "":
                    return "(not set)"
                return "•" * min(len(str(raw)), 16)
            if raw in (None, ""):
                return "(default)"
            return str(raw)

        if field.name.startswith("browser_"):
            raw = self._profile_value_for_field_raw(profile, field)
            if field.name in {"browser_headless", "browser_screenshot_on_error"}:
                return "on" if raw else "off"
            if raw in (None, ""):
                return "(default)"
            return str(raw)

        if field.name.startswith("subagents_"):
            raw = self._profile_value_for_field_raw(profile, field)
            if field.name == "subagents_enabled":
                return "on" if raw else "off"
            if field.name == "subagents_strategy":
                if raw == "serial":
                    return "serial (1 lane)"
                if raw == "slots":
                    return "llama slots"
                if raw == "manual":
                    return "manual width"
                return str(raw)
            if field.name == "subagents_notify_on_finish":
                return "on" if raw else "off"
            if isinstance(raw, (int, float)):
                return str(raw)
            return "(default)"

        raw = self._profile_value_for_field_raw(profile, field)

        # SECRET masks the value for display
        if field.kind == FieldKind.SECRET:
            if raw is None or raw == "":
                return "(not set)"
            return "•" * min(len(str(raw)), 16)

        if raw is None:
            return "(none)"
        return str(raw)

    def _set_field_on_profile(self, profile_idx: int, field: _SettingField, new_value: Any) -> None:
        """Mutate _working_profiles[profile_idx], updating dirty set + preview.

        Handles the simple top-level fields (theme, display_mode,
        density, intro_animation, system_prompt) AND the provider_*
        fields, which mutate the provider dict.
        """
        old_profile = self._working_profiles[profile_idx]

        if field.name == "theme":
            new_profile = replace(old_profile, theme=new_value)
        elif field.name == "display_mode":
            new_profile = replace(old_profile, display_mode=new_value)
        elif field.name == "density":
            new_profile = replace(old_profile, density=new_value)
        elif field.name == "intro_animation":
            new_profile = replace(old_profile, intro_animation=new_value)
        elif field.name == "system_prompt":
            new_profile = replace(old_profile, system_prompt=new_value)
        elif field.name == "tools":
            new_tools = tuple(new_value)
            new_skills = tuple(old_profile.skills)
            added_tools = set(new_tools) - set(old_profile.tools)
            if not new_skills and added_tools.intersection({"holonet", "browser"}):
                new_skills = recommended_skills_for_tools(new_tools)
            new_profile = replace(old_profile, tools=new_tools, skills=new_skills)
        elif field.name == "skills":
            new_profile = replace(old_profile, skills=tuple(new_value))
        elif field.name.startswith("bash_"):
            # Write into a fresh tool_config dict so the compare with
            # the initial snapshot is clean.
            key = field.name.removeprefix("bash_")
            new_tool_config = {
                k: dict(v) if isinstance(v, dict) else v
                for k, v in (old_profile.tool_config or {}).items()
            }
            bash_cfg = dict(new_tool_config.get("bash") or {})
            bash_cfg[key] = new_value
            new_tool_config["bash"] = bash_cfg
            new_profile = replace(old_profile, tool_config=new_tool_config)
        elif field.name.startswith("holonet_"):
            key = field.name.removeprefix("holonet_")
            new_tool_config = {
                k: dict(v) if isinstance(v, dict) else v
                for k, v in (old_profile.tool_config or {}).items()
            }
            holonet_cfg = dict(new_tool_config.get("holonet") or {})
            holonet_cfg[key] = new_value
            new_tool_config["holonet"] = holonet_cfg
            new_profile = replace(old_profile, tool_config=new_tool_config)
        elif field.name.startswith("browser_"):
            key = field.name.removeprefix("browser_")
            new_tool_config = {
                k: dict(v) if isinstance(v, dict) else v
                for k, v in (old_profile.tool_config or {}).items()
            }
            browser_cfg = dict(new_tool_config.get("browser") or {})
            browser_cfg[key] = new_value
            new_tool_config["browser"] = browser_cfg
            new_profile = replace(old_profile, tool_config=new_tool_config)
        elif field.name.startswith("provider_"):
            key = field.name.removeprefix("provider_")
            new_provider = dict(old_profile.provider) if old_profile.provider else {}
            new_provider[key] = new_value
            new_profile = replace(old_profile, provider=new_provider)
        elif field.name.startswith("compaction_"):
            # Build a new CompactionConfig with the updated field. The
            # config is itself a frozen dataclass so we use replace().
            # If the new value violates the invariant (e.g. autocompact_pct
            # set higher than warning_pct), CompactionConfig.__post_init__
            # raises ValueError — we catch it and surface as a toast so
            # the user sees the rejection without crashing the menu.
            from ..profiles import CompactionConfig
            key = field.name.removeprefix("compaction_")
            try:
                new_compaction = replace(old_profile.compaction, **{key: new_value})
            except ValueError as exc:
                self._toast = _Toast(
                    f"invalid compaction value: {exc}",
                    self.elapsed,
                    kind="warn",
                )
                return
            new_profile = replace(old_profile, compaction=new_compaction)
        elif field.name.startswith("subagents_"):
            key = field.name.removeprefix("subagents_")
            try:
                new_subagents = replace(old_profile.subagents, **{key: new_value})
            except ValueError as exc:
                self._toast = _Toast(
                    f"invalid subagent value: {exc}",
                    self.elapsed,
                    kind="warn",
                )
                return
            new_profile = replace(old_profile, subagents=new_subagents)
        else:
            return  # not editable

        self._working_profiles[profile_idx] = new_profile

        # Dirty tracking — compare against initial snapshot
        initial = self._initial_profiles[profile_idx]
        initial_value = self._profile_value_for_field_raw(initial, field)
        if new_value == initial_value:
            self._dirty.discard((new_profile.name, field.name))
        else:
            self._dirty.add((new_profile.name, field.name))

        # If we're editing the profile that's currently displayed in
        # the preview, sync the preview chat AND make sure the cursor
        # isn't stranded on a row that's now hidden (e.g. the bash_*
        # flags disappear when bash is toggled off).
        if profile_idx == self._profile_cursor:
            self._sync_preview()
            visible = self._editable_visible_indices()
            if visible and self._settings_cursor not in visible:
                self._settings_cursor = visible[0]

    def _profile_value_for_field_raw(self, profile: Profile, field: _SettingField) -> Any:
        """Read the RAW value of a field — no truncation, no masking.

        Used for dirty comparison, edit-buffer initialization, and
        anywhere the actual value matters rather than its display form.
        """
        if field.name == "theme":
            return profile.theme
        if field.name == "display_mode":
            return profile.display_mode
        if field.name == "density":
            return profile.density
        if field.name == "intro_animation":
            return profile.intro_animation
        if field.name == "system_prompt":
            return profile.system_prompt
        if field.name == "skills":
            return tuple(profile.skills)
        if field.name == "tools":
            return tuple(profile.tools)
        if field.name.startswith("bash_"):
            # Pull from profile.tool_config["bash"] with defaults
            # mirrored from BashConfig. Missing entries mean "default"
            # so the dirty-compare vs initial is stable.
            from ..bash.exec import (
                BashConfig,
                DEFAULT_TIMEOUT_S,
                MAX_OUTPUT_BYTES,
            )
            bash_cfg = (profile.tool_config or {}).get("bash") or {}
            key = field.name.removeprefix("bash_")
            if key == "allow_dangerous":
                return bool(bash_cfg.get("allow_dangerous", False))
            if key == "allow_mutating":
                return bool(bash_cfg.get("allow_mutating", True))
            if key == "timeout_s":
                return float(bash_cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
            if key == "max_output_bytes":
                return int(bash_cfg.get("max_output_bytes", MAX_OUTPUT_BYTES))
            return None
        if field.name.startswith("provider_"):
            key = field.name.removeprefix("provider_")
            if profile.provider:
                return profile.provider.get(key)
            return None
        if field.name.startswith("holonet_"):
            holonet_cfg = (profile.tool_config or {}).get("holonet") or {}
            key = field.name.removeprefix("holonet_")
            defaults = {
                "default_provider": "auto",
                "brave_enabled": True,
                "brave_api_key": "",
                "brave_api_key_file": "",
                "firecrawl_enabled": True,
                "firecrawl_api_key": "",
                "firecrawl_api_key_file": "",
                "europe_pmc_enabled": True,
                "clinicaltrials_enabled": True,
                "biomedical_enabled": True,
            }
            return holonet_cfg.get(key, defaults.get(key))
        if field.name.startswith("browser_"):
            browser_cfg = (profile.tool_config or {}).get("browser") or {}
            key = field.name.removeprefix("browser_")
            defaults = {
                "headless": True,
                "channel": "chrome",
                "python_executable": "",
                "executable_path": "",
                "user_data_dir": "",
                "viewport_width": 1440,
                "viewport_height": 960,
                "timeout_s": 20.0,
                "screenshot_on_error": True,
            }
            return browser_cfg.get(key, defaults.get(key))
        if field.name.startswith("compaction_"):
            key = field.name.removeprefix("compaction_")
            cfg = profile.compaction
            return getattr(cfg, key, None)
        if field.name.startswith("subagents_"):
            key = field.name.removeprefix("subagents_")
            cfg = profile.subagents
            return getattr(cfg, key, None)
        return None

    def _is_dirty(self, profile_name: str, field_name: str | None = None) -> bool:
        """Check whether a specific (profile, field) or any field of a
        profile is dirty."""
        if field_name is not None:
            return (profile_name, field_name) in self._dirty
        return any(p == profile_name for (p, _) in self._dirty)

    def _any_dirty(self) -> bool:
        return bool(self._dirty)

    # ─── Save / revert ───

    def _save(self) -> None:
        """Write all dirty profiles to disk + sync chat.json for active.

        For the currently-active profile, also writes its theme,
        display_mode, and density into chat.json so the changes take
        effect when the user returns to the chat (without needing to
        clear their saved overrides manually).
        """
        if not self._dirty:
            self._toast = _Toast("nothing to save", self.elapsed, kind="info")
            return

        flash_keys: set[tuple[str, str]] = set(self._dirty)

        try:
            target_dir = config_dir() / "profiles"
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._toast = _Toast(f"save failed: {exc}", self.elapsed, kind="warn")
            return

        # Determine which profiles need to be written: any that have
        # at least one dirty field.
        dirty_profile_names = {p_name for (p_name, _) in self._dirty}
        for i, profile in enumerate(self._working_profiles):
            if profile.name not in dirty_profile_names:
                continue
            try:
                path = target_dir / f"{profile.name}.json"
                path.write_text(
                    json.dumps(_profile_to_json_dict(profile), indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                self._toast = _Toast(
                    f"save failed for '{profile.name}': {exc}",
                    self.elapsed, kind="warn",
                )
                return

        # If the active profile had any appearance fields edited, sync
        # those to chat.json so they take effect on next chat open.
        # The active profile is whichever one is currently in chat.json,
        # NOT necessarily the one under the cursor.
        active_now = get_active_profile()
        active_profile_in_working = None
        for p in self._working_profiles:
            if p.name == active_now.name:
                active_profile_in_working = p
                break

        if active_profile_in_working is not None:
            cfg = load_chat_config()
            changed = False
            for fname in ("theme", "display_mode", "density"):
                if (active_now.name, fname) in self._dirty:
                    cfg[fname] = self._profile_value_for_field_raw(
                        active_profile_in_working,
                        next(f for f in _SETTINGS_TREE if f.name == fname),
                    )
                    changed = True
            if changed:
                save_chat_config(cfg)

        # Reload the registry so subsequent get_profile() calls see
        # the new state
        PROFILE_REGISTRY.reload()

        # Update the initial snapshot to match the new committed state
        self._initial_profiles = [deepcopy(p) for p in self._working_profiles]

        # Clear dirty set
        self._dirty.clear()

        # Trigger the per-field save flash
        self._save_flash = _SaveFlash(field_keys=flash_keys, started_at=self.elapsed)

        # Toast
        n = len(flash_keys)
        plural = "s" if n != 1 else ""
        self._toast = _Toast(
            f"saved {n} change{plural}",
            self.elapsed,
            kind="ok",
        )

    def _revert(self) -> None:
        """Drop all unsaved changes back to the initial snapshot."""
        if not self._dirty:
            self._toast = _Toast("nothing to revert", self.elapsed, kind="info")
            return
        n = len(self._dirty)
        self._working_profiles = [deepcopy(p) for p in self._initial_profiles]
        self._dirty.clear()
        self._sync_preview()
        plural = "s" if n != 1 else ""
        self._toast = _Toast(
            f"reverted {n} change{plural}",
            self.elapsed,
            kind="info",
        )

    # ─── Delete profile ───

    def _begin_delete_confirm(self) -> None:
        """Validate the cursor profile is deletable, then arm the modal.

        Refusal cases (each shows a warning toast and does NOT open the
        modal):
          - Pure built-in (no user file to remove): nothing to delete
          - Currently active per chat.json: would orphan the chat
          - Last remaining profile: would leave nothing to fall back to

        Successful cases open the modal in one of two modes:
          - "delete": pure user profile, JSON file gets unlinked
          - "revert": user override of a built-in, file unlinked and
                      the built-in re-emerges in its place
        """
        if not self._working_profiles:
            return
        idx = self._profile_cursor
        profile = self._working_profiles[idx]

        # Last-profile guard — never let the user nuke their only profile
        if len(self._working_profiles) <= 1:
            self._toast = _Toast(
                "can't delete the last profile",
                self.elapsed, kind="warn",
            )
            return

        # Active-profile guard — refuse if this is the live one in chat.json
        active_now = get_active_profile().name
        if profile.name == active_now:
            self._toast = _Toast(
                f"'{profile.name}' is the active profile — switch first",
                self.elapsed, kind="warn",
            )
            return

        # Determine whether the profile has a user file at all. If only
        # the built-in source exists, there's nothing to delete from disk.
        user_path = config_dir() / "profiles" / f"{profile.name}.json"
        has_user_file = user_path.exists()
        builtin_path = (
            Path(__file__).resolve().parent.parent
            / "builtin" / "profiles" / f"{profile.name}.json"
        )
        has_builtin = builtin_path.exists()

        if not has_user_file and has_builtin:
            self._toast = _Toast(
                f"'{profile.name}' is built-in — nothing to delete",
                self.elapsed, kind="warn",
            )
            return
        if not has_user_file and not has_builtin:
            # Pathological — registry has it but no file backs it.
            self._toast = _Toast(
                f"'{profile.name}' has no file on disk",
                self.elapsed, kind="warn",
            )
            return

        mode = "revert" if has_builtin else "delete"
        self._delete_confirm = _DeleteConfirm(
            profile_idx=idx,
            profile_name=profile.name,
            mode=mode,
            started_at=self.elapsed,
        )

    def _perform_delete(self) -> None:
        """Confirmed — unlink the user file and refresh local state.

        For "revert" mode the built-in re-emerges automatically when we
        reload the registry, so the row stays in the list but reverts
        to its built-in form. For "delete" mode the row vanishes.
        """
        confirm = self._delete_confirm
        if confirm is None:
            return
        target = config_dir() / "profiles" / f"{confirm.profile_name}.json"
        try:
            target.unlink()
        except OSError as exc:
            self._toast = _Toast(
                f"delete failed: {exc}",
                self.elapsed, kind="warn",
            )
            self._delete_confirm = None
            return

        # Drop any dirty markers tied to this profile — they no longer
        # apply because the source file has been removed.
        self._dirty = {
            (p, f) for (p, f) in self._dirty if p != confirm.profile_name
        }

        # Reload the registry and rebuild local snapshots from scratch.
        PROFILE_REGISTRY.reload()
        self._initial_profiles = [deepcopy(p) for p in all_profiles()]
        self._working_profiles = [deepcopy(p) for p in all_profiles()]
        if not self._working_profiles:
            self._working_profiles = [Profile(name="(no profiles loaded)")]
            self._initial_profiles = list(self._working_profiles)

        # Snap the cursor onto a still-existing row. If the deleted
        # profile reappeared (revert mode), prefer it; otherwise clamp.
        new_idx = 0
        for i, p in enumerate(self._working_profiles):
            if p.name == confirm.profile_name:
                new_idx = i
                break
        else:
            new_idx = min(confirm.profile_idx, len(self._working_profiles) - 1)
        self._profile_cursor = max(0, new_idx)

        # Update active_idx to match chat.json (it didn't change but we
        # want the marker dot to land on the right row after the rebuild)
        active = get_active_profile()
        for i, p in enumerate(self._working_profiles):
            if p.name == active.name:
                self._active_idx = i
                break

        self._sync_preview()

        verb = "reverted" if confirm.mode == "revert" else "deleted"
        self._toast = _Toast(
            f"{verb} '{confirm.profile_name}'",
            self.elapsed,
            kind="ok",
        )
        self._delete_confirm = None

    def _handle_delete_confirm_key(self, event: KeyEvent) -> None:
        """Input handling while the delete confirmation modal is open.

        Safe-default key choice: Enter, N, Esc, and Tab all CANCEL.
        Only Y (case-insensitive) actually deletes — this matches every
        sane "are you sure?" dialog and means a tired finger on Enter
        does nothing destructive.
        """
        if event.key == Key.ENTER or event.key == Key.ESC:
            self._delete_confirm = None
            return
        if event.is_char and event.char and not event.is_ctrl and not event.is_alt:
            ch = event.char.lower()
            if ch == "y":
                self._perform_delete()
                return
            if ch == "n":
                self._delete_confirm = None
                return

    # ─── Input ───

    def on_key(self, byte: int) -> None:
        for event in self._key_decoder.feed(byte):
            if isinstance(event, KeyEvent):
                self._handle_key(event)

    def _handle_key(self, event: KeyEvent) -> None:
        # ─── Modal sub-editors take exclusive input first ───

        # Multi-line prompt editor (MULTILINE fields) — full screen modal
        if self._prompt_editor is not None:
            self._prompt_editor.handle_key(event)
            if self._prompt_editor.is_done:
                result = self._prompt_editor.result
                if result is not None:
                    # User saved — commit the new prompt to the profile
                    field = next(
                        f for f in _SETTINGS_TREE if f.name == "system_prompt"
                    )
                    self._set_field_on_profile(self._profile_cursor, field, result)
                self._prompt_editor = None
            return

        # Delete confirmation modal — blocks every other input
        if self._delete_confirm is not None:
            self._handle_delete_confirm_key(event)
            return

        # Inline text/number/secret editor — single-row modal
        if self._inline_text_edit is not None:
            self._handle_inline_text_key(event)
            return

        # Inline cycle-edit overlay
        if self._editing_field is not None:
            self._handle_edit_key(event)
            return

        # Tools multi-select overlay
        if self._tools_edit is not None:
            self._handle_tools_edit_key(event)
            return

        # Esc → exit. If there are unsaved changes, the FIRST esc warns
        # via toast and consumes the keypress; the SECOND exits.
        if event.key == Key.ESC:
            if self._any_dirty() and (
                self._toast is None
                or self._toast.kind != "warn"
                or "unsaved" not in self._toast.text
            ):
                self._toast = _Toast(
                    "unsaved changes — press esc again to discard, or s to save",
                    self.elapsed,
                    kind="warn",
                )
                return
            # Second esc with warning still active → exit and discard
            self.exit_to_chat = True
            self.requested_active_profile = self._current_profile().name
            self.stop()
            return

        # Tab cycles focus
        if event.key == Key.TAB:
            self._focus = Focus.SETTINGS if self._focus == Focus.PROFILES else Focus.PROFILES
            return

        # 's' saves
        if event.is_char and event.char == "s" and not event.is_ctrl and not event.is_alt:
            self._save()
            return

        # 'r' reverts
        if event.is_char and event.char == "r" and not event.is_ctrl and not event.is_alt:
            self._revert()
            return

        # Pane-specific dispatch
        if self._focus == Focus.PROFILES:
            self._handle_profiles_key(event)
        else:
            self._handle_settings_key(event)

    def _handle_profiles_key(self, event: KeyEvent) -> None:
        if event.key == Key.UP:
            self._profile_cursor = (self._profile_cursor - 1) % len(self._working_profiles)
            self._last_profile_switch_at = self.elapsed
            self._sync_preview()
            return
        if event.key == Key.DOWN:
            self._profile_cursor = (self._profile_cursor + 1) % len(self._working_profiles)
            self._last_profile_switch_at = self.elapsed
            self._sync_preview()
            return
        if event.key == Key.RIGHT:
            self._focus = Focus.SETTINGS
            return
        if event.key == Key.ENTER:
            # Enter on a profile selects it as the active profile (the
            # one that the chat will resume with). Exits the menu.
            self.exit_to_chat = True
            self.requested_active_profile = self._working_profiles[self._profile_cursor].name
            # Persist as active immediately so the chat picks it up
            cfg = load_chat_config()
            cfg["active_profile"] = self._working_profiles[self._profile_cursor].name
            save_chat_config(cfg)
            self.stop()
            return
        # Capital D opens the delete-profile confirmation. Lowercase d
        # is reserved for future use; we keep the keybind shifted so a
        # casual hand on the keyboard can't accidentally arm a delete.
        if event.is_char and event.char == "D" and not event.is_ctrl and not event.is_alt:
            self._begin_delete_confirm()
            return

    def _handle_settings_key(self, event: KeyEvent) -> None:
        if event.key == Key.UP:
            self._settings_move(-1)
            return
        if event.key == Key.DOWN:
            self._settings_move(+1)
            return
        if event.key == Key.LEFT:
            self._focus = Focus.PROFILES
            return
        if event.key == Key.ENTER:
            self._begin_edit()
            return

    def _begin_edit(self) -> None:
        """Open the right editor for the cursor field, if editable."""
        field = self._current_field()
        if field.kind == FieldKind.READONLY:
            return

        if field.kind == FieldKind.TOGGLE:
            # Toggle is immediate — no inline overlay needed
            options = field.options_getter() if field.options_getter else []
            if not options or len(options) < 2:
                return
            current = self._profile_value_for_field_raw(self._current_profile(), field)
            try:
                idx = options.index(current)
            except ValueError:
                idx = 0
            new_value = options[(idx + 1) % len(options)]
            self._set_field_on_profile(self._profile_cursor, field, new_value)
            return

        if field.kind == FieldKind.CYCLE:
            options = field.options_getter() if field.options_getter else []
            if not options:
                return
            current = self._profile_value_for_field_raw(self._current_profile(), field)
            try:
                idx = options.index(current)
            except ValueError:
                idx = 0
            self._editing_field = self._settings_cursor
            self._editing_cursor = idx
            self._edit_snapshot = current
            return

        if field.kind in (FieldKind.TEXT, FieldKind.NUMBER, FieldKind.SECRET):
            # Inline single-line text editor in the row itself
            current_raw = self._profile_value_for_field_raw(self._current_profile(), field)
            # Compaction pct fields are stored as fractions (0.0625) but
            # the user edits them as percentages (6.25). Convert here so
            # the buffer matches what the user sees in the row label.
            if (
                current_raw is not None
                and field.name.startswith("compaction_")
                and field.name.endswith("_pct")
            ):
                buffer = f"{float(current_raw) * 100:g}"
            else:
                buffer = "" if current_raw is None else str(current_raw)
            self._inline_text_edit = _InlineTextEdit(
                field_idx=self._settings_cursor,
                buffer=buffer,
                cursor=len(buffer),
                snapshot=current_raw,
                kind=field.kind,
            )
            return

        if field.kind == FieldKind.MULTILINE:
            # Open the full-screen text editor overlay. Pass the
            # terminal's clipboard helper so Ctrl+C copies the
            # selection to the system clipboard via OSC 52 (works in
            # Ghostty/iTerm2/kitty/alacritty/modern xterm).
            current = self._profile_value_for_field_raw(self._current_profile(), field) or ""
            self._prompt_editor = PromptEditor(
                initial=str(current),
                copy_callback=self.term.copy_to_clipboard,
            )
            return

        if field.kind == FieldKind.TOOLS_TOGGLE:
            # Open the multi-select overlay. Snapshot the current tools
            # tuple so Esc can restore it if the user cancels.
            current = self._profile_value_for_field_raw(self._current_profile(), field)
            snapshot = tuple(current) if current else ()
            self._tools_edit = _ToolsEdit(
                field_idx=self._settings_cursor,
                cursor=0,
                snapshot=snapshot,
            )
            return

    def _handle_inline_text_key(self, event: KeyEvent) -> None:
        """Input handling while an inline TEXT/NUMBER/SECRET edit is open.

        Esc cancels (restores snapshot). Enter commits (validates for
        NUMBER fields). Arrow keys + Home/End move cursor. Backspace
        deletes char before cursor. Printable input inserts at cursor.
        """
        edit = self._inline_text_edit
        if edit is None:
            return
        field = _SETTINGS_TREE[edit.field_idx]

        if event.key == Key.ESC:
            # Cancel — restore the snapshot, no dirty change
            self._set_field_on_profile(self._profile_cursor, field, edit.snapshot)
            self._inline_text_edit = None
            return

        if event.key == Key.ENTER:
            # Commit — validate if NUMBER, otherwise just save the buffer
            if field.kind == FieldKind.NUMBER:
                parsed = self._parse_number(edit.buffer, field.number_kind)
                if parsed is None:
                    # Validation failed — flash a warning toast
                    self._toast = _Toast(
                        f"'{edit.buffer}' is not a valid {field.number_kind}",
                        self.elapsed,
                        kind="warn",
                    )
                    return
                # Compaction pct fields: user enters a percent (6.25)
                # which we convert to a fraction (0.0625) before
                # writing to the underlying CompactionConfig.
                if field.name.startswith("compaction_") and field.name.endswith("_pct"):
                    parsed = float(parsed) / 100.0
                self._set_field_on_profile(self._profile_cursor, field, parsed)
            else:
                # TEXT or SECRET — save the buffer as-is
                self._set_field_on_profile(self._profile_cursor, field, edit.buffer)
            self._inline_text_edit = None
            return

        if event.key == Key.LEFT:
            edit.cursor = prev_grapheme_boundary(edit.buffer, edit.cursor)
            return
        if event.key == Key.RIGHT:
            edit.cursor = next_grapheme_boundary(edit.buffer, edit.cursor)
            return
        if event.key == Key.HOME:
            edit.cursor = 0
            return
        if event.key == Key.END:
            edit.cursor = len(edit.buffer)
            return

        if event.key == Key.BACKSPACE:
            if edit.cursor > 0:
                edit.buffer, edit.cursor = delete_prev_grapheme(
                    edit.buffer,
                    edit.cursor,
                )
            return
        if event.key == Key.DELETE:
            if edit.cursor < len(edit.buffer):
                edit.buffer, edit.cursor = delete_next_grapheme(
                    edit.buffer,
                    edit.cursor,
                )
            return

        # Printable input
        if event.is_char and event.char and not event.is_ctrl and not event.is_alt:
            for ch in event.char:
                if ord(ch) >= 0x20 and ch != "\n":
                    # NUMBER fields filter to digits + sign + decimal point
                    if field.kind == FieldKind.NUMBER:
                        if not self._is_valid_number_char(ch, field.number_kind):
                            continue
                    edit.buffer = edit.buffer[: edit.cursor] + ch + edit.buffer[edit.cursor :]
                    edit.cursor += 1

    @staticmethod
    def _parse_number(buffer: str, kind: str) -> Any:
        """Parse a number string, returning the parsed value or None on failure."""
        s = buffer.strip()
        if not s:
            return None
        try:
            if kind == "int":
                return int(s)
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _is_valid_number_char(ch: str, kind: str) -> bool:
        """Allow digits, sign, and (for floats) the decimal point."""
        if ch.isdigit():
            return True
        if ch in ("-", "+"):
            return True
        if kind == "float" and ch in (".", "e", "E"):
            return True
        return False

    def _handle_edit_key(self, event: KeyEvent) -> None:
        """Input handling while an inline cycle-edit is open."""
        field = _SETTINGS_TREE[self._editing_field]  # type: ignore[index]
        options = field.options_getter() if field.options_getter else []
        if not options:
            self._editing_field = None
            return

        if event.key == Key.ESC:
            # Cancel — restore the snapshot value
            self._set_field_on_profile(self._profile_cursor, field, self._edit_snapshot)
            self._editing_field = None
            return
        if event.key == Key.ENTER:
            # Confirm — keep the current edit cursor as the new value
            new_value = options[self._editing_cursor]
            self._set_field_on_profile(self._profile_cursor, field, new_value)
            self._editing_field = None
            return
        if event.key == Key.UP:
            self._editing_cursor = (self._editing_cursor - 1) % len(options)
            # Live preview as the user cycles
            self._set_field_on_profile(
                self._profile_cursor, field, options[self._editing_cursor]
            )
            return
        if event.key == Key.DOWN:
            self._editing_cursor = (self._editing_cursor + 1) % len(options)
            self._set_field_on_profile(
                self._profile_cursor, field, options[self._editing_cursor]
            )
            return

    def _handle_tools_edit_key(self, event: KeyEvent) -> None:
        """Input handling while the tools multi-select overlay is open.

        ↑↓ moves the cursor between tools. Space toggles the currently
        highlighted tool on or off (updates the live profile immediately
        so the dirty marker and preview refresh in real time). Enter
        commits the working selection and closes the overlay. Esc
        restores the snapshot and closes the overlay — all intermediate
        toggles are undone.
        """
        edit = self._tools_edit
        if edit is None:
            return
        field = _SETTINGS_TREE[edit.field_idx]
        option_names = self._multi_select_names(field)
        if not option_names:
            self._tools_edit = None
            return

        if event.key == Key.ESC:
            # Cancel — revert to the pre-edit snapshot
            self._set_field_on_profile(self._profile_cursor, field, edit.snapshot)
            self._tools_edit = None
            return
        if event.key == Key.ENTER:
            # Commit — current profile tools already reflect the working
            # state (we toggle live). Just close.
            self._tools_edit = None
            return
        if event.key == Key.UP:
            edit.cursor = (edit.cursor - 1) % len(option_names)
            return
        if event.key == Key.DOWN:
            edit.cursor = (edit.cursor + 1) % len(option_names)
            return
        if event.is_char and event.char == " ":
            # Toggle the highlighted tool on/off in the live profile
            name = option_names[edit.cursor]
            current = list(
                self._profile_value_for_field_raw(self._current_profile(), field) or ()
            )
            if name in current:
                current.remove(name)
            else:
                current.append(name)
            self._set_field_on_profile(self._profile_cursor, field, tuple(current))
            return

    # ─── Render ───

    def on_tick(self, grid: Grid) -> None:
        # Drain decoder
        for event in self._key_decoder.flush():
            if isinstance(event, KeyEvent):
                self._handle_key(event)

        rows, cols = grid.rows, grid.cols
        if rows < 16 or cols < 80:
            self._paint_too_small(grid, rows, cols)
            return

        # Theme variant for chrome — use the cursor profile so the
        # menu paints itself in the same palette as the preview
        chrome_theme = find_theme_or_fallback(self._current_profile().theme)
        chrome_variant = chrome_theme.variant(
            normalize_display_mode(self._current_profile().display_mode)
        )

        fill_region(grid, 0, 0, cols, rows, style=Style(bg=chrome_variant.bg))

        # ─── Title row ───
        self._paint_title(grid, chrome_variant, cols)

        # ─── Layout pane widths ───
        # Three panes + 2 vertical separators (1 cell each)
        avail_w = cols - 2  # account for separators
        left_w = max(MIN_LEFT_W, int(cols * 0.18))
        right_w = max(MIN_RIGHT_W, int(cols * 0.36))
        # Ensure middle is at least MIN_MIDDLE_W
        middle_w = avail_w - left_w - right_w
        if middle_w < MIN_MIDDLE_W:
            # Steal from the wider neighbor
            deficit = MIN_MIDDLE_W - middle_w
            if right_w - deficit >= MIN_RIGHT_W:
                right_w -= deficit
            else:
                left_w = max(MIN_LEFT_W, left_w - deficit)
            middle_w = avail_w - left_w - right_w

        body_top = 1
        body_bottom = rows - 1  # 1 row footer

        left_x = 0
        sep1_x = left_x + left_w
        middle_x = sep1_x + 1
        sep2_x = middle_x + middle_w
        right_x = sep2_x + 1

        # ─── Pane backgrounds ───
        fill_region(grid, left_x, body_top, left_w, body_bottom - body_top,
                    style=Style(bg=chrome_variant.bg_input))
        fill_region(grid, middle_x, body_top, middle_w, body_bottom - body_top,
                    style=Style(bg=chrome_variant.bg))
        fill_region(grid, right_x, body_top, cols - right_x, body_bottom - body_top,
                    style=Style(bg=chrome_variant.bg))

        # ─── Vertical separators ───
        sep_style = Style(fg=chrome_variant.fg_subtle, bg=chrome_variant.bg)
        for sy in range(body_top, body_bottom):
            grid.set(sy, sep1_x, Cell("│", sep_style))
            grid.set(sy, sep2_x, Cell("│", sep_style))

        # ─── Pane content ───
        self._paint_profiles_pane(
            grid, chrome_variant,
            x=left_x, y=body_top,
            w=left_w, h=body_bottom - body_top,
        )
        self._paint_settings_pane(
            grid, chrome_variant,
            x=middle_x, y=body_top,
            w=middle_w, h=body_bottom - body_top,
        )
        self._paint_preview_pane(
            grid, chrome_variant,
            x=right_x, y=body_top,
            w=cols - right_x, h=body_bottom - body_top,
        )

        # ─── Footer ───
        if rows >= 2:
            self._paint_footer(grid, chrome_variant, y=rows - 1, width=cols)

        # ─── Inline cycle-edit overlay ───
        if self._editing_field is not None:
            self._paint_edit_overlay(
                grid, chrome_variant,
                middle_x=middle_x, middle_w=middle_w,
                body_top=body_top, body_bottom=body_bottom,
            )

        # ─── Tools multi-select overlay ───
        if self._tools_edit is not None:
            self._paint_tools_edit_overlay(
                grid, chrome_variant,
                middle_x=middle_x, middle_w=middle_w,
                body_top=body_top, body_bottom=body_bottom,
            )

        # ─── Prompt editor overlay (MULTILINE fields) ───
        # Painted AFTER the cycle-edit overlay so a prompt edit, if
        # opened from inside the menu, sits on top of everything else.
        # Toasts go above the prompt editor since save/cancel feedback
        # should still be visible.
        if self._prompt_editor is not None:
            # Take ~80% of the screen, centered
            ed_w = max(60, int(cols * 0.85))
            ed_h = max(14, int(rows * 0.85))
            ed_x = (cols - ed_w) // 2
            ed_y = (rows - ed_h) // 2
            self._prompt_editor.paint(
                grid,
                x=ed_x, y=ed_y, w=ed_w, h=ed_h,
                theme=chrome_variant,
            )

        # ─── Delete confirmation modal ───
        # Sits on top of everything except the toast (so the user can
        # still see the result of their action immediately after).
        if self._delete_confirm is not None:
            self._paint_delete_confirm_overlay(grid, chrome_variant, cols, rows)

        # ─── Toast (overlays EVERYTHING, even the prompt editor) ───
        self._paint_toast(grid, chrome_variant, cols, rows)

    def _paint_too_small(self, grid: Grid, rows: int, cols: int) -> None:
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=0x000000))
        if rows < 1 or cols < 6:
            return
        candidates = (
            "terminal too small for config — needs at least 80×16",
            "too small — needs 80×16",
            "too small (80x16)",
            "too small",
        )
        msg = candidates[-1]
        for c in candidates:
            if len(c) <= cols:
                msg = c
                break
        mx = max(0, (cols - len(msg)) // 2)
        my = max(0, rows // 2)
        paint_text(
            grid, msg, mx, my,
            style=Style(fg=0xCCCCCC, bg=0x000000, attrs=ATTR_BOLD),
        )

    # ─── Title bar ───

    def _paint_title(self, grid: Grid, theme: ThemeVariant, cols: int) -> None:
        title = "successor · config"
        title_style = Style(fg=theme.fg, bg=theme.bg, attrs=ATTR_BOLD)
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

        # Right-anchored: dirty count pill if any unsaved changes
        if self._any_dirty():
            n = len(self._dirty)
            label = f"  {n} unsaved  "
            label_style = Style(
                fg=theme.bg, bg=theme.accent_warm, attrs=ATTR_BOLD,
            )
            lx = max(0, cols - len(label))
            paint_text(grid, label, lx, 0, style=label_style)

    # ─── Profiles pane ───

    def _paint_profiles_pane(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        # Header
        header_style = Style(fg=theme.fg_dim, bg=theme.bg_input, attrs=ATTR_DIM)
        paint_text(grid, "  profiles", x, y + 1, style=header_style)

        # Underline
        underline = "─" * (w - 2)
        paint_text(
            grid, underline, x + 1, y + 2,
            style=Style(fg=theme.fg_subtle, bg=theme.bg_input),
        )

        # Focus border (top edge of pane)
        if self._focus == Focus.PROFILES:
            border_color = self._pulse_color(theme.accent, theme.fg)
            border_style = Style(fg=border_color, bg=theme.bg_input, attrs=ATTR_BOLD)
            border_text = "▔" * w
            paint_text(grid, border_text, x, y, style=border_style)
        else:
            paint_text(
                grid, "▔" * w, x, y,
                style=Style(fg=theme.fg_subtle, bg=theme.bg_input, attrs=ATTR_DIM),
            )

        # Get the active profile name (per chat.json) for the ● marker
        active_now = get_active_profile().name

        list_top = y + 4
        for i, profile in enumerate(self._working_profiles):
            row_y = list_top + i
            if row_y >= y + h - 1:
                break

            is_cursor = i == self._profile_cursor and self._focus == Focus.PROFILES
            is_active_in_config = i == self._active_idx
            is_active_now = profile.name == active_now
            has_dirty = self._is_dirty(profile.name)

            # Get this profile's theme accent for the swatch
            try:
                p_theme = find_theme_or_fallback(profile.theme)
                p_accent = p_theme.variant(
                    normalize_display_mode(profile.display_mode)
                ).accent
            except Exception:
                p_accent = theme.accent

            # Selection bar (when this row is the cursor under PROFILES focus)
            if is_cursor:
                fill_region(
                    grid, x, row_y, w, 1,
                    style=Style(bg=theme.accent),
                )

            # Cursor glyph
            cursor_glyph = "▸" if is_cursor else " "
            glyph_style = Style(
                fg=theme.bg if is_cursor else theme.fg_subtle,
                bg=theme.accent if is_cursor else theme.bg_input,
                attrs=ATTR_BOLD,
            )
            paint_text(grid, cursor_glyph, x + 1, row_y, style=glyph_style)

            # Color swatch (filled square in this profile's accent color)
            swatch_style = Style(
                fg=p_accent,
                bg=theme.accent if is_cursor else theme.bg_input,
                attrs=ATTR_BOLD,
            )
            paint_text(grid, "●", x + 3, row_y, style=swatch_style)

            # Profile name
            name_text = profile.name
            max_name_w = w - 8  # leave room for cursor + swatch + markers
            if len(name_text) > max_name_w:
                name_text = name_text[: max_name_w - 1] + "…"
            name_style = Style(
                fg=theme.bg if is_cursor else theme.fg,
                bg=theme.accent if is_cursor else theme.bg_input,
                attrs=ATTR_BOLD,
            )
            paint_text(grid, name_text, x + 5, row_y, style=name_style)

            # Right-side markers: ● for active, * for dirty
            markers = ""
            if has_dirty:
                markers += "*"
            if is_active_now:
                markers += "●"
            if markers:
                marker_x = x + w - len(markers) - 1
                marker_style = Style(
                    fg=theme.bg if is_cursor else (
                        theme.accent_warn if has_dirty else theme.accent_warm
                    ),
                    bg=theme.accent if is_cursor else theme.bg_input,
                    attrs=ATTR_BOLD,
                )
                paint_text(grid, markers, marker_x, row_y, style=marker_style)

    # ─── Settings pane ───

    def _paint_settings_pane(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        # Header
        header_style = Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM)
        paint_text(grid, "  settings", x, y + 1, style=header_style)
        underline = "─" * (w - 2)
        paint_text(
            grid, underline, x + 1, y + 2,
            style=Style(fg=theme.fg_subtle, bg=theme.bg),
        )

        # Focus border (top edge)
        if self._focus == Focus.SETTINGS:
            border_color = self._pulse_color(theme.accent, theme.fg)
            border_style = Style(fg=border_color, bg=theme.bg, attrs=ATTR_BOLD)
            paint_text(grid, "▔" * w, x, y, style=border_style)
        else:
            paint_text(
                grid, "▔" * w, x, y,
                style=Style(fg=theme.fg_subtle, bg=theme.bg, attrs=ATTR_DIM),
            )

        # Render the settings tree
        profile = self._current_profile()
        list_top = y + 4
        cur_y = list_top
        last_section: str | None = None

        # Compute label column width: longest field label, padded
        label_col_w = max(len(f.label) for f in _SETTINGS_TREE)

        for idx, fld in enumerate(_SETTINGS_TREE):
            # Skip fields not applicable to this profile (e.g. bash_*
            # flags when bash isn't enabled)
            if not _field_visible_for_profile(profile, fld):
                continue

            # Section header
            if fld.section and fld.section != last_section:
                if cur_y >= y + h - 1:
                    break
                cur_y += 1  # spacer
                if cur_y >= y + h - 1:
                    break
                paint_text(
                    grid, fld.section, x + 2, cur_y,
                    style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_BOLD | ATTR_DIM),
                )
                cur_y += 1
                last_section = fld.section

            if cur_y >= y + h - 1:
                break

            # Cursor highlight for the focused field
            is_inline_text_editing = (
                self._inline_text_edit is not None
                and self._inline_text_edit.field_idx == idx
            )
            is_tools_editing = (
                self._tools_edit is not None
                and self._tools_edit.field_idx == idx
            )
            is_cursor = (
                idx == self._settings_cursor
                and self._focus == Focus.SETTINGS
                and self._editing_field is None
                and not is_inline_text_editing
                and not is_tools_editing
            )
            is_being_edited = (
                idx == self._editing_field or is_inline_text_editing
                or is_tools_editing
            )

            # Background fill for cursor row
            if is_cursor or is_being_edited:
                fill_region(
                    grid, x + 1, cur_y, w - 2, 1,
                    style=Style(
                        bg=theme.accent if is_cursor else theme.accent_warm,
                    ),
                )

            # Read the value
            value = self._profile_value_for_field(profile, fld)
            if value is None:
                value_text = "(none)"
            else:
                value_text = str(value)

            # Dirty marker
            dirty = self._is_dirty(profile.name, fld.name)
            dirty_marker = "*" if dirty else " "

            # Save flash check — for the next SAVE_FLASH_S after a save,
            # the saved fields pulse green
            flash_t = 0.0
            if self._save_flash is not None:
                elapsed_flash = self.elapsed - self._save_flash.started_at
                if elapsed_flash >= SAVE_FLASH_S:
                    self._save_flash = None
                else:
                    if (profile.name, fld.name) in self._save_flash.field_keys:
                        flash_t = 1.0 - (elapsed_flash / SAVE_FLASH_S)

            # Resolve label color
            if fld.kind == FieldKind.READONLY:
                label_color = theme.fg_subtle
                label_attrs = ATTR_DIM | ATTR_ITALIC
            elif is_cursor:
                label_color = theme.bg
                label_attrs = ATTR_BOLD
            else:
                label_color = theme.fg_dim
                label_attrs = 0

            label_bg = (
                theme.accent if is_cursor
                else theme.accent_warm if is_being_edited
                else theme.bg
            )

            # Label
            paint_text(
                grid, fld.label.rjust(label_col_w),
                x + 4, cur_y,
                style=Style(fg=label_color, bg=label_bg, attrs=label_attrs),
            )

            # Dirty marker
            paint_text(
                grid, dirty_marker,
                x + 4 + label_col_w + 1, cur_y,
                style=Style(
                    fg=theme.accent_warn if dirty else label_bg,
                    bg=label_bg,
                    attrs=ATTR_BOLD,
                ),
            )

            # Value
            value_x = x + 4 + label_col_w + 3
            value_color = label_color
            if flash_t > 0:
                # Save flash — lerp from accent_warm (green-ish if forge,
                # blue-ish if steel) toward the normal color over the
                # flash duration. Use a green if available, otherwise
                # the warm accent.
                green = 0x33CC55  # cheap "saved" green that reads on any bg
                value_color = lerp_rgb(label_color, green, flash_t)
            elif fld.kind != FieldKind.READONLY and not is_cursor and not is_being_edited:
                value_color = theme.fg

            max_value_w = max(0, w - (value_x - x) - 2)

            # If this row is being inline-text-edited, draw the buffer
            # + cursor instead of the static value text
            if is_inline_text_editing and self._inline_text_edit is not None:
                edit = self._inline_text_edit
                # SECRET fields render the buffer in plaintext while
                # editing (so the user can verify what they typed) but
                # display masked when NOT editing — this matches every
                # desktop password field's behavior.
                buf = edit.buffer
                # Soft-clip horizontally if the buffer is longer than the cell
                if len(buf) > max_value_w:
                    # Show the tail (where the cursor probably is)
                    buf_display = "…" + buf[-(max_value_w - 1):]
                    cursor_in_display = max_value_w - 1 - (len(buf) - edit.cursor)
                else:
                    buf_display = buf
                    cursor_in_display = edit.cursor

                paint_text(
                    grid, buf_display, value_x, cur_y,
                    style=Style(
                        fg=theme.bg, bg=theme.accent_warm,
                        attrs=ATTR_BOLD,
                    ),
                )

                # Cursor blink — invert one cell at the cursor position
                blink_visible = (int(self.elapsed * 3) % 2) == 0
                if blink_visible and 0 <= cursor_in_display <= len(buf_display):
                    cursor_x = value_x + cursor_in_display
                    if cursor_x < x + w - 1:
                        # Read the char at cursor (or space if past end)
                        ch = (
                            buf_display[cursor_in_display]
                            if cursor_in_display < len(buf_display)
                            else " "
                        )
                        grid.set(
                            cur_y, cursor_x,
                            Cell(ch, Style(
                                fg=theme.accent_warm, bg=theme.bg,
                                attrs=ATTR_BOLD,
                            )),
                        )
            else:
                if len(value_text) > max_value_w:
                    value_text = value_text[: max(0, max_value_w - 1)] + "…"
                paint_text(
                    grid, value_text, value_x, cur_y,
                    style=Style(
                        fg=value_color, bg=label_bg,
                        attrs=ATTR_BOLD if (is_cursor or is_being_edited) else 0,
                    ),
                )

            # Hint for read-only fields
            if fld.kind == FieldKind.READONLY and fld.hint:
                hint_x = value_x + len(value_text) + 2
                if hint_x < x + w - len(fld.hint) - 1:
                    paint_text(
                        grid, f"({fld.hint})", hint_x, cur_y,
                        style=Style(
                            fg=theme.fg_subtle, bg=label_bg,
                            attrs=ATTR_DIM | ATTR_ITALIC,
                        ),
                    )

            cur_y += 1

    # ─── Preview pane ───

    def _paint_preview_pane(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        # Header
        header_style = Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM)
        paint_text(grid, "  preview", x, y + 1, style=header_style)
        paint_text(
            grid, "─" * (w - 2), x + 1, y + 2,
            style=Style(fg=theme.fg_subtle, bg=theme.bg),
        )

        # Top border (no focus possible on preview, so always dim)
        paint_text(
            grid, "▔" * w, x, y,
            style=Style(fg=theme.fg_subtle, bg=theme.bg, attrs=ATTR_DIM),
        )

        # Render the preview chat into a sub-grid
        preview_top = y + 4
        preview_h = h - 4
        if preview_h < 6 or w < 30:
            return

        sub = Grid(preview_h, w)
        try:
            self._preview_chat.on_tick(sub)
        except Exception:
            return

        for sy in range(sub.rows):
            dst_y = preview_top + sy
            if dst_y >= grid.rows:
                break
            for sx in range(sub.cols):
                dst_x = x + sx
                if dst_x >= grid.cols:
                    break
                cell = sub.at(sy, sx)
                if cell.wide_tail:
                    continue
                grid.set(dst_y, dst_x, cell)

    # ─── Footer ───

    def _paint_footer(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        y: int,
        width: int,
    ) -> None:
        fill_region(grid, 0, y, width, 1, style=Style(bg=theme.bg_footer))

        if self._prompt_editor is not None:
            keybinds = "↑↓←→ navigate · ⏎ newline · ⌫ delete · Ctrl+S save · esc cancel"
        elif self._delete_confirm is not None:
            verb = "revert" if self._delete_confirm.mode == "revert" else "delete"
            keybinds = f"Y {verb} · N/⏎/esc cancel"
        elif self._inline_text_edit is not None:
            edit = self._inline_text_edit
            kind_name = edit.kind.value
            keybinds = f"editing {kind_name} · type to input · ←→ cursor · ⏎ confirm · esc cancel"
        elif self._editing_field is not None:
            keybinds = "↑↓ pick · ⏎ confirm · esc cancel"
        elif self._tools_edit is not None:
            keybinds = "↑↓ move · space toggle · ⏎ confirm · esc cancel"
        elif self._focus == Focus.PROFILES:
            keybinds = "tab focus · ↑↓ profile · ⏎ activate · → settings · D delete · s save · r revert · esc back"
        else:
            keybinds = "tab focus · ↑↓ field · ⏎ edit · ← profiles · s save · r revert · esc back"

        # Truncate keybinds if too long
        if len(keybinds) > width - 2:
            keybinds = keybinds[: width - 3] + "…"
        paint_text(
            grid, keybinds, 1, y,
            style=Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_DIM),
        )

    # ─── Toast ───

    def _paint_toast(
        self,
        grid: Grid,
        theme: ThemeVariant,
        cols: int,
        rows: int,
    ) -> None:
        if self._toast is None:
            return
        elapsed = self.elapsed - self._toast.started_at
        if elapsed >= TOAST_DURATION_S:
            self._toast = None
            return

        glyph = "✓" if self._toast.kind == "ok" else "⚠" if self._toast.kind == "warn" else "ℹ"
        text = f"  {glyph} {self._toast.text}  "
        text_w = len(text)

        slide_in_s = 0.2
        if elapsed < slide_in_s:
            t = ease_out_cubic(elapsed / slide_in_s)
            offscreen_x = cols
            target_x = cols - text_w - 2
            tx = int(offscreen_x + (target_x - offscreen_x) * t)
        else:
            tx = cols - text_w - 2

        # Fade out over the last 500ms
        fade_out_s = 0.5
        time_until_end = TOAST_DURATION_S - elapsed
        if time_until_end < fade_out_s:
            fade_t = time_until_end / fade_out_s
        else:
            fade_t = 1.0

        if self._toast.kind == "warn":
            base_bg = theme.accent_warn
        elif self._toast.kind == "info":
            base_bg = theme.fg_dim
        else:
            base_bg = theme.accent

        bg_color = lerp_rgb(theme.bg, base_bg, fade_t)
        fg_color = lerp_rgb(theme.bg, theme.fg, fade_t) if self._toast.kind == "info" else lerp_rgb(theme.bg_footer, theme.fg, fade_t)

        ty = 1
        if ty >= rows or tx < 0:
            return
        paint_text(
            grid, text, tx, ty,
            style=Style(fg=fg_color, bg=bg_color, attrs=ATTR_BOLD),
        )

    # ─── Inline edit overlay ───

    def _paint_edit_overlay(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        middle_x: int,
        middle_w: int,
        body_top: int,
        body_bottom: int,
    ) -> None:
        """Paint the inline cycle-edit list over the settings pane.

        The overlay is anchored just below the row being edited so the
        user has visual continuity between the field they're changing
        and the option list. If anchoring below would clip the bottom
        of the pane, the overlay flips to anchor ABOVE the row instead.
        """
        if self._editing_field is None:
            return
        field = _SETTINGS_TREE[self._editing_field]
        options = field.options_getter() if field.options_getter else []
        if not options:
            return

        # Compute box dimensions
        max_opt_w = max(len(str(o) if o is not None else "(none)") for o in options)
        box_w = min(middle_w - 4, max(20, max_opt_w + 8))
        box_h = min(body_bottom - body_top - 2, len(options) + 2)

        # Anchor the box to the row being edited. We need to compute
        # the screen y of that row, which means walking the same paint
        # loop the settings pane uses (header + section spacers).
        list_top = body_top + 4
        cur_y = list_top
        last_section: str | None = None
        editing_row_y = list_top
        for idx, fld in enumerate(_SETTINGS_TREE):
            if fld.section and fld.section != last_section:
                cur_y += 2  # spacer + section header row
                last_section = fld.section
            if idx == self._editing_field:
                editing_row_y = cur_y
                break
            cur_y += 1

        # Position: just below the row, 4 cells right of the label start
        box_x = middle_x + 4
        if box_x + box_w > middle_x + middle_w - 1:
            box_x = middle_x + middle_w - box_w - 1
        box_y = editing_row_y + 1

        # If the box would clip the bottom, flip above the row
        if box_y + box_h > body_bottom - 1:
            box_y = editing_row_y - box_h
            if box_y < body_top + 1:
                # Neither below nor above fits — fall back to centered
                box_y = body_top + max(1, (body_bottom - body_top - box_h) // 2)

        # Draw the box
        border_style = Style(fg=theme.accent_warm, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style, chars=BOX_ROUND,
        )

        # Header
        header = f" {field.label} "
        paint_text(
            grid, header, box_x + 2, box_y,
            style=Style(fg=theme.bg, bg=theme.accent_warm, attrs=ATTR_BOLD),
        )

        # Options
        for i, opt in enumerate(options):
            row_y = box_y + 1 + i
            if row_y >= box_y + box_h - 1:
                break
            opt_text = str(opt) if opt is not None else "(none)"
            is_selected = i == self._editing_cursor

            row_bg = theme.accent if is_selected else theme.bg_input
            row_fg = theme.bg if is_selected else theme.fg
            fill_region(
                grid, box_x + 1, row_y, box_w - 2, 1,
                style=Style(bg=row_bg),
            )
            cursor_glyph = "▸" if is_selected else " "
            paint_text(
                grid, f" {cursor_glyph} {opt_text}",
                box_x + 1, row_y,
                style=Style(fg=row_fg, bg=row_bg, attrs=ATTR_BOLD),
            )

    # ─── Tools multi-select overlay ───

    def _paint_tools_edit_overlay(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        middle_x: int,
        middle_w: int,
        body_top: int,
        body_bottom: int,
    ) -> None:
        """Paint the tools multi-select list over the settings pane.

        Mirrors `_paint_edit_overlay` (cycle overlay) in placement so
        the user's spatial model stays consistent. Each tool gets a
        checkbox reflecting its current enabled state in the live
        profile — toggling updates the profile as the user works.
        """
        edit = self._tools_edit
        if edit is None:
            return
        field = _SETTINGS_TREE[edit.field_idx]
        option_names = self._multi_select_names(field)
        if not option_names:
            return

        # Compute the overlay box dimensions. Each row shows the
        # checkbox + label + short description, so we want a wider box
        # than the cycle overlay.
        longest_desc = max(
            len(self._multi_select_label_desc(field, n)[1]) for n in option_names
        )
        longest_label = max(len(self._multi_select_label_desc(field, n)[0]) for n in option_names)
        desired_w = longest_label + longest_desc + 12
        box_w = min(middle_w - 4, max(32, desired_w))
        box_h = min(body_bottom - body_top - 2, len(option_names) + 4)

        # Anchor to the row being edited — same walk as the cycle overlay
        list_top = body_top + 4
        cur_y = list_top
        last_section: str | None = None
        editing_row_y = list_top
        for idx, fld in enumerate(_SETTINGS_TREE):
            if fld.section and fld.section != last_section:
                cur_y += 2
                last_section = fld.section
            if idx == edit.field_idx:
                editing_row_y = cur_y
                break
            cur_y += 1

        box_x = middle_x + 4
        if box_x + box_w > middle_x + middle_w - 1:
            box_x = middle_x + middle_w - box_w - 1
        box_y = editing_row_y + 1
        if box_y + box_h > body_bottom - 1:
            box_y = editing_row_y - box_h
            if box_y < body_top + 1:
                box_y = body_top + max(1, (body_bottom - body_top - box_h) // 2)

        border_style = Style(fg=theme.accent_warm, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style, chars=BOX_ROUND,
        )

        # Header pill
        header = f" {field.label} — space toggles "
        paint_text(
            grid, header, box_x + 2, box_y,
            style=Style(fg=theme.bg, bg=theme.accent_warm, attrs=ATTR_BOLD),
        )

        # Current enabled set (read live from the profile so toggles
        # reflect immediately)
        enabled = set(self._profile_value_for_field_raw(self._current_profile(), field) or ())

        # Rows
        for i, name in enumerate(option_names):
            row_y = box_y + 1 + i
            if row_y >= box_y + box_h - 1:
                break
            label, desc = self._multi_select_label_desc(field, name)
            is_cursor = i == edit.cursor
            is_on = name in enabled
            check = "[✓]" if is_on else "[ ]"
            row_bg = theme.accent if is_cursor else theme.bg_input
            row_fg = theme.bg if is_cursor else theme.fg
            fill_region(
                grid, box_x + 1, row_y, box_w - 2, 1,
                style=Style(bg=row_bg),
            )
            cursor_glyph = "▸" if is_cursor else " "
            row_text = f" {cursor_glyph} {check}  {label}"
            paint_text(
                grid, row_text, box_x + 1, row_y,
                style=Style(fg=row_fg, bg=row_bg, attrs=ATTR_BOLD),
            )
            # Dim description trailing the label
            desc_x = box_x + 1 + len(row_text) + 2
            max_desc_w = max(0, box_x + box_w - 2 - desc_x)
            if len(desc) > max_desc_w:
                desc = desc[: max(0, max_desc_w - 1)] + "…"
            desc_fg = theme.bg if is_cursor else theme.fg_dim
            paint_text(
                grid, desc, desc_x, row_y,
                style=Style(fg=desc_fg, bg=row_bg, attrs=ATTR_DIM | ATTR_ITALIC),
            )

    # ─── Delete confirmation overlay ───

    def _paint_delete_confirm_overlay(
        self,
        grid: Grid,
        theme: ThemeVariant,
        cols: int,
        rows: int,
    ) -> None:
        """Paint the centered 'delete profile?' modal.

        Layout:
            ╭─ delete profile? ──────────────────╮
            │                                    │
            │   forge-dev                        │
            │   forge · dark · spacious          │
            │                                    │
            │   ⚠ this deletes the JSON file     │
            │     from disk and can't be undone  │
            │                                    │
            │       Y delete    N/⏎/esc cancel   │
            ╰────────────────────────────────────╯

        Theme-aware via accent_warn for the border + the warning glyph.
        Fades in over 200ms via lerp_rgb so it doesn't punch into the
        screen — same easing as the toast animation.
        """
        confirm = self._delete_confirm
        if confirm is None:
            return

        # Fade-in animation: 200ms ease, then hold
        fade_s = 0.2
        elapsed = self.elapsed - confirm.started_at
        fade_t = ease_out_cubic(min(1.0, elapsed / fade_s)) if fade_s > 0 else 1.0

        profile = self._working_profiles[confirm.profile_idx]
        is_revert = confirm.mode == "revert"

        # Build the lines we need to fit so we can size the box.
        title = " revert profile? " if is_revert else " delete profile? "
        sub_lines: list[tuple[str, str]] = []  # (text, role)
        sub_lines.append(("", "spacer"))
        sub_lines.append((profile.name, "name"))
        meta = f"{profile.theme} · {profile.display_mode} · {profile.density}"
        if profile.intro_animation:
            meta += f" · {profile.intro_animation}"
        sub_lines.append((meta, "meta"))
        sub_lines.append(("", "spacer"))
        if is_revert:
            sub_lines.append(("⚠ this removes your override and", "warn1"))
            sub_lines.append(("  reverts to the built-in version", "warn2"))
        else:
            sub_lines.append(("⚠ this deletes the JSON file from", "warn1"))
            sub_lines.append(("  disk and can't be undone", "warn2"))
        sub_lines.append(("", "spacer"))
        action_word = "revert" if is_revert else "delete"
        action_line = f"Y {action_word}    N/⏎/esc cancel"
        sub_lines.append((action_line, "actions"))

        # Box dimensions — fit to longest line, +6 padding
        content_w = max(len(title) - 2, max(len(t) for t, _ in sub_lines))
        box_w = min(cols - 4, content_w + 6)
        box_h = len(sub_lines) + 2  # top/bottom border

        if box_w < 20 or box_h + 2 > rows:
            return

        box_x = (cols - box_w) // 2
        box_y = max(1, (rows - box_h) // 2)

        # Color blends — accent_warn is the danger color, but we soften
        # it a touch by lerping toward bg as the modal fades in.
        border_color = lerp_rgb(theme.bg, theme.accent_warn, fade_t)
        fill_bg = lerp_rgb(theme.bg, theme.bg_input, fade_t)
        title_fg = lerp_rgb(theme.bg, theme.bg, fade_t)  # title sits on accent_warn
        title_bg = border_color

        border_style = Style(fg=border_color, bg=fill_bg, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=fill_bg)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style, chars=BOX_ROUND,
        )

        # Title pill in the top border, slightly inset
        title_x = box_x + 3
        if title_x + len(title) < box_x + box_w - 1:
            paint_text(
                grid, title, title_x, box_y,
                style=Style(fg=title_fg, bg=title_bg, attrs=ATTR_BOLD),
            )

        # Body lines
        for i, (text, role) in enumerate(sub_lines):
            row_y = box_y + 1 + i
            if row_y >= box_y + box_h - 1:
                break
            if not text:
                continue

            # Indent — name/meta/warn rows are 3 cells in; actions are centered
            if role == "actions":
                tx = box_x + (box_w - len(text)) // 2
            else:
                tx = box_x + 3

            if role == "name":
                color = lerp_rgb(theme.bg, theme.fg, fade_t)
                attrs = ATTR_BOLD
            elif role == "meta":
                color = lerp_rgb(theme.bg, theme.fg_dim, fade_t)
                attrs = ATTR_DIM
            elif role in ("warn1", "warn2"):
                color = lerp_rgb(theme.bg, theme.accent_warn, fade_t)
                attrs = ATTR_BOLD if role == "warn1" else 0
            elif role == "actions":
                color = lerp_rgb(theme.bg, theme.fg, fade_t)
                attrs = ATTR_BOLD
            else:
                color = lerp_rgb(theme.bg, theme.fg_dim, fade_t)
                attrs = 0

            paint_text(
                grid, text, tx, row_y,
                style=Style(fg=color, bg=fill_bg, attrs=attrs),
            )

    # ─── Helpers ───

    def _pulse_color(self, base: int, target: int) -> int:
        """Gentle ambient pulse between two colors at BORDER_PULSE_HZ."""
        t = 0.5 + 0.5 * math.sin(self.elapsed * BORDER_PULSE_HZ * 2 * math.pi)
        return lerp_rgb(base, target, t * 0.4)


# ─── Profile JSON serialization ───


def _profile_to_json_dict(profile: Profile) -> dict:
    """Serialize a Profile back to its JSON file format.

    Mirrors the parser in profiles/profile.py — every field gets
    written, even defaults, so the resulting file is fully self-
    documenting and editable by hand.
    """
    return {
        "name": profile.name,
        "description": profile.description,
        "theme": profile.theme,
        "display_mode": profile.display_mode,
        "density": profile.density,
        "system_prompt": profile.system_prompt,
        "provider": dict(profile.provider) if profile.provider else None,
        "skills": list(profile.skills),
        "tools": list(profile.tools),
        "tool_config": dict(profile.tool_config),
        "intro_animation": profile.intro_animation,
        "chat_intro_art": profile.chat_intro_art,
        "compaction": profile.compaction.to_dict(),
        "subagents": profile.subagents.to_dict(),
    }


# ─── Public entry point ───


def run_config_menu() -> str | None:
    """Run the config menu interactively. Returns the active profile
    name the user wants the chat to resume with, or None on cancel.

    The menu may have written profile JSON files and/or chat.json
    during the session — those side effects persist regardless of
    the return value.
    """
    menu = SuccessorConfig()
    menu.run()
    if menu.exit_to_chat:
        return menu.requested_active_profile
    return None
