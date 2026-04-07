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
  │                     │     model       qwopus          │ │                  │ │
  │                     │     base_url    localhost…      │ │ ctx 48/256k qw…  │ │
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
        options_getter=lambda: [None, "nusamurai"],
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
        kind=FieldKind.READONLY, hint="phase 5 not yet wired",
    ),
    _SettingField(
        name="tools", label="tools", section="",
        kind=FieldKind.READONLY, hint="phase 6 not yet wired",
    ),
)

# Indices into _SETTINGS_TREE that ARE editable (used for cursor jumps)
_EDITABLE_INDICES = tuple(
    i for i, f in enumerate(_SETTINGS_TREE) if f.kind != FieldKind.READONLY
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

        # ─── Multi-line prompt editor (MULTILINE fields) ───
        self._prompt_editor: PromptEditor | None = None

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
        from ..demos.chat import SuccessorChat, _Message

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

        from ..demos.chat import find_density, NORMAL
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

    def _first_editable_at_or_after(self, start_idx: int) -> int:
        """Find the first editable row index at or after start_idx."""
        for i in range(start_idx, len(_SETTINGS_TREE)):
            if _SETTINGS_TREE[i].kind != FieldKind.READONLY:
                return i
        # Wrap
        for i in range(0, start_idx):
            if _SETTINGS_TREE[i].kind != FieldKind.READONLY:
                return i
        return 0

    def _settings_move(self, delta: int) -> None:
        """Move the settings cursor by delta, skipping read-only rows."""
        if not _EDITABLE_INDICES:
            return
        # Find current position in the editable indices list
        current = self._settings_cursor
        try:
            cur_pos = _EDITABLE_INDICES.index(current)
        except ValueError:
            cur_pos = 0
        new_pos = (cur_pos + delta) % len(_EDITABLE_INDICES)
        self._settings_cursor = _EDITABLE_INDICES[new_pos]
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
            return f"({len(profile.skills)})"
        if field.name == "tools":
            return f"({len(profile.tools)})"

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
        elif field.name.startswith("provider_"):
            key = field.name.removeprefix("provider_")
            new_provider = dict(old_profile.provider) if old_profile.provider else {}
            new_provider[key] = new_value
            new_profile = replace(old_profile, provider=new_provider)
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
        # the preview, sync the preview chat
        if profile_idx == self._profile_cursor:
            self._sync_preview()

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
        if field.name.startswith("provider_"):
            key = field.name.removeprefix("provider_")
            if profile.provider:
                return profile.provider.get(key)
            return None
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

        # Inline text/number/secret editor — single-row modal
        if self._inline_text_edit is not None:
            self._handle_inline_text_key(event)
            return

        # Inline cycle-edit overlay
        if self._editing_field is not None:
            self._handle_edit_key(event)
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
                self._set_field_on_profile(self._profile_cursor, field, parsed)
            else:
                # TEXT or SECRET — save the buffer as-is
                self._set_field_on_profile(self._profile_cursor, field, edit.buffer)
            self._inline_text_edit = None
            return

        if event.key == Key.LEFT:
            edit.cursor = max(0, edit.cursor - 1)
            return
        if event.key == Key.RIGHT:
            edit.cursor = min(len(edit.buffer), edit.cursor + 1)
            return
        if event.key == Key.HOME:
            edit.cursor = 0
            return
        if event.key == Key.END:
            edit.cursor = len(edit.buffer)
            return

        if event.key == Key.BACKSPACE:
            if edit.cursor > 0:
                edit.buffer = edit.buffer[: edit.cursor - 1] + edit.buffer[edit.cursor :]
                edit.cursor -= 1
            return
        if event.key == Key.DELETE:
            if edit.cursor < len(edit.buffer):
                edit.buffer = edit.buffer[: edit.cursor] + edit.buffer[edit.cursor + 1 :]
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
            is_cursor = (
                idx == self._settings_cursor
                and self._focus == Focus.SETTINGS
                and self._editing_field is None
                and not is_inline_text_editing
            )
            is_being_edited = (
                idx == self._editing_field or is_inline_text_editing
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
        elif self._inline_text_edit is not None:
            edit = self._inline_text_edit
            kind_name = edit.kind.value
            keybinds = f"editing {kind_name} · type to input · ←→ cursor · ⏎ confirm · esc cancel"
        elif self._editing_field is not None:
            keybinds = "↑↓ pick · ⏎ confirm · esc cancel"
        elif self._focus == Focus.PROFILES:
            keybinds = "tab focus · ↑↓ profile · ⏎ activate · → settings · s save · r revert · esc back"
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
