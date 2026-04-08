"""SuccessorSetup — the profile creation wizard.

Multi-region App that guides the user through creating a new profile
with a LIVE preview pane on the right. The preview pane is a real
SuccessorChat instance that the wizard mutates as the user picks options;
when they arrow between themes, the preview chat's existing
_set_theme()/blend_variants machinery animates the swap in real time.

Layout (resize-aware, all coordinates derived from grid.rows/cols):

  ┌─────────────────────────────────────────────────────────────────┐
  │                          successor · setup                          │  title (1 row)
  ├──────────────┬──────────────────────────────────────────────────┤
  │              │                                                  │
  │  ✓ welcome   │  step 3 of 8 — choose your color theme           │  step heading
  │  ✓ name      │                                                  │
  │  ▸ theme     │  ▸ steel    ◆  cool blue, instrument panel       │  options
  │    mode      │    forge    ▲  warm red                          │
  │    density   │                                                  │
  │    intro     │  description: instrument-panel oklch — cool      │  detail
  │    review    │  blue accents, ported from ComPress              │
  │              │                                                  │
  │              │  live preview ─────────────────────────────      │
  │              │  ┌──────────────────────────────────────┐        │
  │              │  │ successor · chat        normal ☾ ◆ steel │        │  preview pane
  │              │  │                                      │        │  (sub-rendered)
  │              │  │ successor                                │        │
  │              │  │ I am successor. The forge is hot.        │        │
  │              │  └──────────────────────────────────────┘        │
  │              │                                                  │
  ├──────────────┴──────────────────────────────────────────────────┤
  │ ↑↓ choose · → next · ← back · esc cancel  step 3/8 ▆▆▆░░░░░     │  footer
  └─────────────────────────────────────────────────────────────────┘

Renderer capabilities exercised (concepts.md categories):

  Cat 1 — Mutable cells: the preview pane is a live sub-rendered
          SuccessorChat that updates every frame as the user picks options.
          The chat's existing _set_theme/_set_density machinery runs
          the smooth transitions for free.

  Cat 2 — Smooth animation: the active step in the sidebar pulses
          gently (1Hz). Welcome screen has a typewriter intro text
          fading in. Validation errors trigger a brief red glow on
          the input field. Section transitions slide in from below.
          Save toast slides in from top-right and fades out.

  Cat 3 — Multi-region UI: sidebar + main + preview + footer all
          stacked in one frame. No z-order fights. Resize works for
          free because every coordinate is computed from grid.rows
          and grid.cols.

  Cat 5 — Replayable, deterministic: the wizard is testable headlessly
          via wizard_demo_snapshot — every step has a snapshot fixture.
          A Player can replay a recording to verify save flows.

  Cat 6 — Inline media: the welcome screen paints a bundled
          successor intro frame above the typewriter text.

  Cat 7 — Programmatic UI: when the user advances steps, the wizard
          drives an automatic spotlight + scroll on the new active
          step in the sidebar. When saving, the wizard transitions
          into the chat with the new profile already active.

The wizard is the proof that the harness is general enough to build
itself. Writing it required ZERO new primitives — every line uses
something the renderer already had.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from ..config import save_chat_config, load_chat_config
from ..graphemes import delete_prev_grapheme
from ..input.keys import (
    Key,
    KeyDecoder,
    KeyEvent,
)
from ..loader import config_dir
from ..profiles import (
    PROFILE_REGISTRY,
    Profile,
    get_active_profile,
    get_profile,
)
from ..render.app import App
from ..render.braille import BrailleArt, load_frame
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
from ..tools_registry import (
    AVAILABLE_TOOLS,
    default_enabled_tools,
)


# ─── Constants ───

# Sidebar width — wide enough for the longest step name + glyph + margin
SIDEBAR_W = 16

# Wizard step pulse rate — gentle, ~1Hz so it reads as breathing not flicker
PULSE_HZ = 0.7

# Welcome screen typewriter speed (chars per second)
WELCOME_TYPEWRITER_CPS = 35.0

# Section reveal animation duration (seconds) — when advancing steps
SECTION_REVEAL_S = 0.22

# Validation glow duration (seconds) — when input is rejected
VALIDATION_GLOW_S = 0.5

# Toast notification duration (seconds) — for "profile saved" etc.
TOAST_DURATION_S = 2.5

# Auto-advance from the saved screen to the chat (seconds)
SAVED_AUTO_ADVANCE_S = 1.6

# Maximum profile name length (also enforces filesystem-safety)
MAX_NAME_LEN = 32

# Default values for fields the wizard doesn't ask about — these get
# baked into the saved profile and can be edited in the JSON afterward.
# Kept in sync with src/successor/builtin/profiles/default.json so the
# wizard-generated profile and the bundled default profile share the
# same starting prompt; users who want something different open
# /config and edit the system_prompt field in the multiline editor.
_DEFAULT_SYSTEM_PROMPT = (
    "You are running inside successor, a terminal chat harness. The "
    "interface renders your replies live with full markdown: headers, "
    "lists, code fences, blockquotes, inline code, and links all paint "
    "correctly in the chat surface. Use them when they help clarity.\n\n"
    "Be direct and brief. Lead with the answer, not the throat-clearing. "
    "Skip filler labels like \"Sure!\", \"Of course\", \"Great question\", "
    "\"Solution:\", \"Verification:\", \"Note:\", or trailing checkmark "
    "summaries. If a topic genuinely needs multiple distinct points, use "
    "a list; if it doesn't, write a sentence.\n\n"
    "If bash tool calls are available you may use them to read files, run "
    "quick checks, or verify your work before answering. Cite file paths "
    "as `file.py:123` when discussing code so the user can navigate. Show "
    "your reasoning when it helps the user follow along, hide it when it "
    "doesn't."
)

_DEFAULT_PROVIDER = {
    "type": "llamacpp",
    "base_url": "http://localhost:8080",
    "model": "local",
    "max_tokens": 32768,
    "temperature": 0.7,
}


# ─── Step machine ───


class Step(Enum):
    """The wizard's linear step sequence.

    Order matters — the sidebar paints steps top-to-bottom in the
    enum's declaration order, and Right/Enter advances to the next
    enum value.
    """
    WELCOME = 0
    NAME = 1
    THEME = 2
    MODE = 3
    DENSITY = 4
    INTRO = 5
    PROVIDER = 6
    TOOLS = 7
    COMPACTION = 8
    REVIEW = 9
    SAVED = 10  # terminal screen — auto-advances into the chat


# Display labels for the sidebar
_STEP_LABELS: dict[Step, str] = {
    Step.WELCOME: "welcome",
    Step.NAME: "name",
    Step.THEME: "theme",
    Step.MODE: "mode",
    Step.DENSITY: "density",
    Step.INTRO: "intro",
    Step.PROVIDER: "provider",
    Step.TOOLS: "tools",
    Step.COMPACTION: "compact",
    Step.REVIEW: "review",
    Step.SAVED: "saved",
}

# Steps that appear in the sidebar (saved is terminal — not shown)
_SIDEBAR_STEPS: tuple[Step, ...] = (
    Step.WELCOME,
    Step.NAME,
    Step.THEME,
    Step.MODE,
    Step.DENSITY,
    Step.INTRO,
    Step.PROVIDER,
    Step.TOOLS,
    Step.COMPACTION,
    Step.REVIEW,
)

# Density options shown in the density step
_DENSITY_OPTIONS: tuple[str, ...] = ("compact", "normal", "spacious")

# Display mode options
_MODE_OPTIONS: tuple[str, ...] = ("dark", "light")

# Intro animation options
_INTRO_OPTIONS: tuple[tuple[str | None, str], ...] = (
    (None, "(none) — chat opens immediately"),
    ("successor", "successor emergence — braille portrait (~5s)"),
)

# Compaction presets shown in the COMPACTION step. Each tuple is
# (key, label, description, CompactionConfig instance).
#
# The "default" preset matches CompactionConfig() defaults exactly so
# users who pick it get the same behavior as users on profiles created
# before this step existed.
#
# "aggressive" compacts much earlier — useful for slow models or for
# preserving headroom on tight context windows.
#
# "lazy" defers compaction until the very last moment — useful when
# you'd rather lose context history than pay the compaction cost early.
#
# "off" disables proactive compaction entirely. Reactive PTL recovery
# still saves you from API rejections, but the chat will run hot.
def _compaction_presets():
    """Lazy import + construction so the module imports cheaply."""
    from ..profiles import CompactionConfig
    return (
        ("default", "default — 12.5% / 6.25% / 1.5%",
         "fire warning at ~12% headroom, autocompact at ~6%, refuse API at ~1.5%",
         CompactionConfig()),
        ("aggressive", "aggressive — 25% / 12.5% / 3%",
         "compact early so you never feel the slow-down at the edge of the window",
         CompactionConfig(
            warning_pct=0.25,
            autocompact_pct=0.125,
            blocking_pct=0.03,
         )),
        ("lazy", "lazy — 5% / 2% / 0.5%",
         "defer compaction as long as possible, keeping more verbatim history in flight",
         CompactionConfig(
            warning_pct=0.05,
            autocompact_pct=0.02,
            blocking_pct=0.005,
            warning_floor=2_000,
            autocompact_floor=1_000,
            blocking_floor=500,
         )),
        ("off", "off — never autocompact",
         "no proactive compaction; reactive PTL recovery still catches API limits",
         CompactionConfig(enabled=False)),
    )


# ─── Wizard state ───


@dataclass
class _WizardState:
    """The user's in-progress profile choices.

    Mutated as the user advances through steps. Defaults are populated
    so the user can skip steps and still get a working profile out the
    other side. Converted to a Profile via .to_profile() at save time.
    """
    name: str = ""
    theme_name: str = "steel"
    display_mode: str = "dark"
    density: str = "normal"
    # Default to the bundled "successor" intro for both: the emergence
    # animation plays when the chat opens, and the bundled hero art
    # serves as the empty-state panel until the user sends their
    # first message. Both are fully skippable from inside the chat
    # (any keypress aborts the intro; submitting a message replaces
    # the empty state). The wizard's INTRO step lets the user opt
    # out before saving if they want a quieter profile.
    intro_animation: str | None = "successor"
    chat_intro_art: str | None = "successor"
    enabled_tools: tuple[str, ...] = field(default_factory=default_enabled_tools)
    # Provider configuration (collected by Step.PROVIDER):
    #   provider_kind  — "llamacpp" (local), "openai" (api.openai.com),
    #                    or "openrouter" (hosted aggregator)
    #   provider_api_key — only used when provider_kind != "llamacpp"
    #   provider_model — model id; for openai, the OpenAI model id
    #                    (e.g. "gpt-4o-mini"); for openrouter, the
    #                    OpenRouter slug (e.g. "openai/gpt-oss-20b:free");
    #                    for llamacpp, a label string ("local" by default
    #                    — llama.cpp ignores it)
    provider_kind: str = "llamacpp"
    provider_api_key: str = ""
    provider_model: str = "openai/gpt-oss-20b:free"
    # Selected compaction preset key — one of:
    #   "default" / "aggressive" / "lazy" / "off"
    # The preset is resolved to a real CompactionConfig at to_profile()
    # time via _compaction_presets().
    compaction_preset: str = "default"

    def _build_provider_dict(self) -> dict:
        """Construct the provider config dict from the wizard state.

        Local llamacpp uses the historical default. OpenAI and OpenRouter
        both use the openai_compat client with provider-specific defaults
        for base_url and model. context_window is intentionally NOT set —
        the chat detects it from the provider on first use (OpenRouter's
        /v1/models for OpenRouter, the hardcoded fallback table for OpenAI).
        """
        if self.provider_kind == "openrouter":
            return {
                "type": "openai_compat",
                "base_url": "https://openrouter.ai/api/v1",
                "model": self.provider_model.strip() or "openai/gpt-oss-20b:free",
                "api_key": self.provider_api_key.strip(),
                "max_tokens": 4096,
                "temperature": 0.7,
            }
        if self.provider_kind == "openai":
            return {
                "type": "openai_compat",
                "base_url": "https://api.openai.com/v1",
                "model": self.provider_model.strip() or "gpt-4o-mini",
                "api_key": self.provider_api_key.strip(),
                "max_tokens": 4096,
                "temperature": 0.7,
            }
        return dict(_DEFAULT_PROVIDER)

    def _resolve_compaction(self):
        """Look up the user's selected preset and return its config."""
        for key, _label, _desc, cfg in _compaction_presets():
            if key == self.compaction_preset:
                return cfg
        # Unknown preset name — fall back to default
        from ..profiles import CompactionConfig
        return CompactionConfig()

    def to_profile(self) -> Profile:
        """Build the final Profile dataclass from the user's choices."""
        return Profile(
            name=self.name.strip().lower() or "untitled",
            description=f"created via successor setup",
            theme=self.theme_name,
            display_mode=self.display_mode,
            density=self.density,
            system_prompt=_DEFAULT_SYSTEM_PROMPT,
            provider=self._build_provider_dict(),
            skills=(),
            tools=tuple(self.enabled_tools),
            tool_config={},
            intro_animation=self.intro_animation,
            chat_intro_art=self.chat_intro_art,
            compaction=self._resolve_compaction(),
        )

    def to_json_dict(self) -> dict:
        """Serialize for the saved JSON file (matches builtin profile shape)."""
        return {
            "name": self.name.strip().lower() or "untitled",
            "description": "created via successor setup",
            "theme": self.theme_name,
            "display_mode": self.display_mode,
            "density": self.density,
            "system_prompt": _DEFAULT_SYSTEM_PROMPT,
            "provider": self._build_provider_dict(),
            "skills": [],
            "tools": list(self.enabled_tools),
            "tool_config": {},
            "intro_animation": self.intro_animation,
            "chat_intro_art": self.chat_intro_art,
            "compaction": self._resolve_compaction().to_dict(),
        }


@dataclass
class _Toast:
    """A transient notification slide-in from the top-right corner."""
    text: str
    started_at: float


@dataclass
class _ValidationGlow:
    """A brief red pulse over an input field to signal a rejected value."""
    field: str  # which input is glowing — e.g. "name"
    message: str
    started_at: float


# ─── Welcome braille frame helper ───


def _try_load_welcome_frame() -> BrailleArt | None:
    """Best-effort load of the successor title frame for the welcome screen.

    Returns None if the asset isn't available — the welcome screen
    falls back to text-only in that case. The frame lives at
    `src/successor/builtin/intros/successor/10-title.txt`, the same
    held title frame the intro animation ends on.
    """
    from ..loader import builtin_root
    candidate = builtin_root() / "intros" / "successor" / "10-title.txt"
    if not candidate.exists():
        return None
    try:
        return BrailleArt(load_frame(candidate))
    except Exception:
        return None


# ─── The wizard App ───


class SuccessorSetup(App):
    """Multi-step profile creation wizard with live preview.

    Subclass of App so it shares the renderer's frame loop, double
    buffering, resize handling, and signal-safe terminal restore.
    Constructed via `run_setup_wizard()` which handles the post-save
    transition into the chat App with the new profile active.
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
        # show up even if a previous import auto-loaded with stale data.
        THEME_REGISTRY.reload()
        PROFILE_REGISTRY.reload()

        # ─── Wizard state ───
        self.state = _WizardState()
        self.current_step: Step = Step.WELCOME
        # Cursor positions per step (e.g. selected theme index, selected
        # mode index, etc.). Stored as a dict keyed by step so each
        # step's cursor survives back-and-forth navigation.
        self._cursors: dict[Step, int] = {
            Step.THEME: 0,
            Step.MODE: 0,
            Step.DENSITY: 1,  # default to "normal"
            Step.INTRO: 1,    # default to "successor" — matches state default
            Step.PROVIDER: 0,  # 0 = type toggle, 1 = api_key, 2 = model
            Step.TOOLS: 0,
            Step.COMPACTION: 0,  # default to "default" preset
        }

        # ─── Animation state ───
        # Time the current step was entered (for section reveal animation)
        self._step_entered_at: float = 0.0
        # Active toast notification, if any
        self._toast: _Toast | None = None
        # Active validation glow, if any
        self._glow: _ValidationGlow | None = None
        # Saved-to-chat transition state — when the user saves, we set
        # this to the time of the save action and auto-advance after
        # SAVED_AUTO_ADVANCE_S into the chat App.
        self._saved_at: float | None = None
        # Profile that was saved — held so the post-wizard chat startup
        # can pick it up
        self.saved_profile: Profile | None = None
        # If True, after this App's run() returns, the caller should
        # construct and run SuccessorChat with `saved_profile`. False means
        # the user cancelled or no save happened.
        self.should_launch_chat: bool = False

        # ─── Welcome screen typewriter state ───
        # The text typewrites from left to right at WELCOME_TYPEWRITER_CPS.
        # Holds the start time so the wizard can compute how many chars
        # are visible per frame.
        self._welcome_started_at: float = 0.0
        self._welcome_frame: BrailleArt | None = _try_load_welcome_frame()

        # ─── Live preview chat ───
        # Constructed once. We mutate its theme/mode/density as the
        # user picks options, then call its on_tick into a sub-grid
        # each wizard frame and copy cells into our main content area.
        # The preview chat's existing transition machinery animates
        # blends for free — we don't reimplement anything.
        self._preview_chat = self._build_preview_chat()

        # ─── Input parsing ───
        self._key_decoder = KeyDecoder()

    # ─── Preview chat construction ───

    def _build_preview_chat(self):
        """Build a fresh SuccessorChat configured to render the wizard's preview.

        Imports happen here (not at module level) to avoid an import
        cycle — chat.py imports from profiles, profiles is imported
        by the wizard, and the wizard wants to render a chat.
        """
        from ..chat import SuccessorChat, _Message

        # Construct the chat WITHOUT a real client (the preview never
        # talks to a model) and WITHOUT a profile (we set state directly).
        # The terminal=Terminal() default is fine because we never enter
        # the chat's terminal context — we only call on_tick on it.
        chat = SuccessorChat()

        # Replace the synthetic greeting with a stable preview script
        # so the preview pane doesn't say "the forge is cold" if
        # llama.cpp isn't running. The script is short — fits comfortably
        # in the smallest preview pane sizes.
        chat.messages = []
        chat.messages.append(
            _Message(
                "successor",
                "Greetings, traveler. I am successor — the blade rests; "
                "the fire is warm. Speak.",
                synthetic=True,
            )
        )
        chat.messages.append(_Message("user", "what's your blade?"))
        chat.messages.append(
            _Message(
                "successor",
                "Patience and intent. Steel without those is just metal."
                " The blade is the silence between heartbeats.",
                synthetic=True,
            )
        )

        # Apply the wizard's initial state to the preview
        chat.theme = find_theme_or_fallback(self.state.theme_name)
        chat.display_mode = self.state.display_mode

        # Density: look up by name through the chat's helper
        from ..chat import find_density, NORMAL
        d = find_density(self.state.density) or NORMAL
        chat.density = d

        return chat

    def _sync_preview_to_state(self) -> None:
        """Apply the wizard's current state to the preview chat.

        Uses the preview chat's existing _set_theme/_set_display_mode/
        _set_density methods so the smooth transition machinery runs
        for free — no animation code in the wizard at all.
        """
        target_theme = find_theme_or_fallback(self.state.theme_name)
        if target_theme.name != self._preview_chat.theme.name:
            self._preview_chat._set_theme(target_theme)
        if self.state.display_mode != self._preview_chat.display_mode:
            self._preview_chat._set_display_mode(self.state.display_mode)
        from ..chat import find_density, NORMAL
        target_density = find_density(self.state.density) or NORMAL
        if target_density.name != self._preview_chat.density.name:
            self._preview_chat._set_density(target_density)

    # ─── Step navigation ───

    def _advance_step(self) -> None:
        """Move to the next step in enum order.

        REVIEW → SAVED transitions through the save action.
        SAVED auto-advances into the chat after SAVED_AUTO_ADVANCE_S.
        """
        if self.current_step == Step.REVIEW:
            self._save_and_finish()
            return
        order = list(Step)
        idx = order.index(self.current_step)
        if idx + 1 < len(order):
            self._enter_step(order[idx + 1])

    def _retreat_step(self) -> None:
        """Move to the previous step in enum order. No-op at WELCOME."""
        order = list(Step)
        idx = order.index(self.current_step)
        if idx > 0:
            self._enter_step(order[idx - 1])

    def _enter_step(self, step: Step) -> None:
        """Mutate state to enter a new step. Resets the reveal clock."""
        self.current_step = step
        self._step_entered_at = self.elapsed
        if step == Step.WELCOME:
            self._welcome_started_at = self.elapsed

    # ─── Save action ───

    def _save_and_finish(self) -> None:
        """Validate, save the profile, persist active_profile, set up chat handoff."""
        # Final validation — name must be non-empty and unique
        name = self.state.name.strip().lower()
        if not name:
            self._glow = _ValidationGlow(
                field="name",
                message="profile name is required",
                started_at=self.elapsed,
            )
            self._enter_step(Step.NAME)
            return

        # Build the JSON payload and write it
        target_dir = config_dir() / "profiles"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._toast = _Toast(
                text=f"save failed: {exc}",
                started_at=self.elapsed,
            )
            return

        target_path = target_dir / f"{name}.json"
        try:
            target_path.write_text(
                json.dumps(self.state.to_json_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self._toast = _Toast(
                text=f"save failed: {exc}",
                started_at=self.elapsed,
            )
            return

        # Persist as active profile so the chat opens with it
        cfg = load_chat_config()
        cfg["active_profile"] = name
        save_chat_config(cfg)

        # Tell the registry to pick up the new file so subsequent
        # get_profile() calls find it
        PROFILE_REGISTRY.reload()

        self.saved_profile = self.state.to_profile()
        self.should_launch_chat = True
        self._toast = _Toast(
            text=f"profile saved as '{name}'",
            started_at=self.elapsed,
        )
        self._saved_at = self.elapsed
        self._enter_step(Step.SAVED)

    # ─── Input ───

    def on_key(self, byte: int) -> None:
        """Decode bytes into KeyEvents and dispatch to the active step."""
        for event in self._key_decoder.feed(byte):
            if isinstance(event, KeyEvent):
                self._handle_key(event)

    def _handle_key(self, event: KeyEvent) -> None:
        # Esc cancels the wizard from any non-saved step
        if event.key == Key.ESC and self.current_step != Step.SAVED:
            self.should_launch_chat = False
            self.stop()
            return

        # Universal navigation
        if self.current_step == Step.SAVED:
            return  # SAVED is non-interactive, auto-advances

        # Step-specific dispatch
        handler_map: dict[Step, Callable[[KeyEvent], None]] = {
            Step.WELCOME: self._handle_welcome,
            Step.NAME: self._handle_name,
            Step.THEME: self._handle_theme,
            Step.MODE: self._handle_mode,
            Step.DENSITY: self._handle_density,
            Step.INTRO: self._handle_intro,
            Step.PROVIDER: self._handle_provider,
            Step.TOOLS: self._handle_tools,
            Step.COMPACTION: self._handle_compaction,
            Step.REVIEW: self._handle_review,
        }
        handler = handler_map.get(self.current_step)
        if handler:
            handler(event)

    def _handle_welcome(self, event: KeyEvent) -> None:
        """Welcome screen: any non-Esc key advances to the name step."""
        if event.key == Key.ENTER or event.key == Key.RIGHT or event.is_char:
            self._advance_step()

    def _handle_name(self, event: KeyEvent) -> None:
        """Name step: text input + Enter to advance, Backspace to delete."""
        if event.key == Key.ENTER:
            name = self.state.name.strip()
            if not name:
                self._glow = _ValidationGlow(
                    field="name",
                    message="name cannot be empty",
                    started_at=self.elapsed,
                )
                return
            if not self._is_valid_name(name):
                self._glow = _ValidationGlow(
                    field="name",
                    message="name must be alphanumeric (+ - _ allowed)",
                    started_at=self.elapsed,
                )
                return
            self._advance_step()
            return
        if event.key == Key.BACKSPACE:
            if self.state.name:
                self.state.name, _ = delete_prev_grapheme(
                    self.state.name,
                    len(self.state.name),
                )
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return
        if event.is_char and event.char and not event.is_ctrl and not event.is_alt:
            # Append printable characters, capped at MAX_NAME_LEN
            for ch in event.char:
                if len(self.state.name) >= MAX_NAME_LEN:
                    self._glow = _ValidationGlow(
                        field="name",
                        message=f"max length is {MAX_NAME_LEN}",
                        started_at=self.elapsed,
                    )
                    break
                if ord(ch) >= 0x20 and ch != " ":
                    self.state.name += ch

    @staticmethod
    def _is_valid_name(name: str) -> bool:
        """Profile names must be alphanumeric + - _ to play nicely with filesystems."""
        if not name:
            return False
        for ch in name:
            if not (ch.isalnum() or ch in "-_"):
                return False
        return True

    def _handle_theme(self, event: KeyEvent) -> None:
        themes = all_themes()
        if not themes:
            if event.key == Key.ENTER or event.key == Key.RIGHT:
                self._advance_step()
            elif event.key == Key.LEFT:
                self._retreat_step()
            return
        cursor = self._cursors[Step.THEME]
        if event.key == Key.UP:
            cursor = (cursor - 1) % len(themes)
            self._cursors[Step.THEME] = cursor
            self.state.theme_name = themes[cursor].name
            self._sync_preview_to_state()
            return
        if event.key == Key.DOWN:
            cursor = (cursor + 1) % len(themes)
            self._cursors[Step.THEME] = cursor
            self.state.theme_name = themes[cursor].name
            self._sync_preview_to_state()
            return
        if event.key == Key.ENTER or event.key == Key.RIGHT:
            self._advance_step()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return

    def _handle_mode(self, event: KeyEvent) -> None:
        cursor = self._cursors[Step.MODE]
        if event.key == Key.UP or event.key == Key.DOWN:
            cursor = 1 - cursor  # toggle 0↔1
            self._cursors[Step.MODE] = cursor
            self.state.display_mode = _MODE_OPTIONS[cursor]
            self._sync_preview_to_state()
            return
        if event.key == Key.ENTER or event.key == Key.RIGHT:
            self._advance_step()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return
        # Space also toggles for ergonomics
        if event.is_char and event.char == " ":
            cursor = 1 - cursor
            self._cursors[Step.MODE] = cursor
            self.state.display_mode = _MODE_OPTIONS[cursor]
            self._sync_preview_to_state()

    def _handle_density(self, event: KeyEvent) -> None:
        cursor = self._cursors[Step.DENSITY]
        if event.key == Key.UP:
            cursor = (cursor - 1) % len(_DENSITY_OPTIONS)
            self._cursors[Step.DENSITY] = cursor
            self.state.density = _DENSITY_OPTIONS[cursor]
            self._sync_preview_to_state()
            return
        if event.key == Key.DOWN:
            cursor = (cursor + 1) % len(_DENSITY_OPTIONS)
            self._cursors[Step.DENSITY] = cursor
            self.state.density = _DENSITY_OPTIONS[cursor]
            self._sync_preview_to_state()
            return
        if event.key == Key.ENTER or event.key == Key.RIGHT:
            self._advance_step()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return

    def _handle_intro(self, event: KeyEvent) -> None:
        cursor = self._cursors[Step.INTRO]
        if event.key == Key.UP or event.key == Key.DOWN:
            cursor = 1 - cursor
            self._cursors[Step.INTRO] = cursor
            self.state.intro_animation = _INTRO_OPTIONS[cursor][0]
            return
        if event.key == Key.ENTER or event.key == Key.RIGHT:
            self._advance_step()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return
        if event.is_char and event.char == " ":
            cursor = 1 - cursor
            self._cursors[Step.INTRO] = cursor
            self.state.intro_animation = _INTRO_OPTIONS[cursor][0]

    def _handle_compaction(self, event: KeyEvent) -> None:
        """Compaction step: cycle through the four presets.

        ↑↓ moves the cursor through (default, aggressive, lazy, off).
        Space also cycles. → / Enter advances to REVIEW. ← retreats
        to TOOLS.
        """
        presets = _compaction_presets()
        cursor = self._cursors[Step.COMPACTION]
        if event.key == Key.UP:
            cursor = (cursor - 1) % len(presets)
            self._cursors[Step.COMPACTION] = cursor
            self.state.compaction_preset = presets[cursor][0]
            return
        if event.key == Key.DOWN:
            cursor = (cursor + 1) % len(presets)
            self._cursors[Step.COMPACTION] = cursor
            self.state.compaction_preset = presets[cursor][0]
            return
        if event.is_char and event.char == " ":
            cursor = (cursor + 1) % len(presets)
            self._cursors[Step.COMPACTION] = cursor
            self.state.compaction_preset = presets[cursor][0]
            return
        if event.key == Key.ENTER or event.key == Key.RIGHT:
            self._advance_step()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return

    def _handle_provider(self, event: KeyEvent) -> None:
        """Provider step: pick local llama.cpp, OpenAI, or OpenRouter.

        Three input rows:
          row 0 — provider type toggle (llama.cpp / openai / openrouter)
          row 1 — api_key field          (only used by openai/openrouter)
          row 2 — model name field       (only used by openai/openrouter)

        ↑↓ moves focus between visible rows. When focus is on row 0,
        Space cycles the provider type. When focus is on rows 1/2,
        printable input fills the field and Backspace deletes. → and
        Enter advance to TOOLS once any required fields are non-empty.
        """
        focus = self._cursors[Step.PROVIDER]
        needs_keys = self.state.provider_kind in ("openrouter", "openai")
        max_focus = 2 if needs_keys else 0  # llamacpp has only the toggle row

        if event.key == Key.UP:
            self._cursors[Step.PROVIDER] = max(0, focus - 1)
            return
        if event.key == Key.DOWN:
            self._cursors[Step.PROVIDER] = min(max_focus, focus + 1)
            return
        if event.key == Key.LEFT:
            # On the toggle row, ← retreats. On the input rows, ←
            # would clobber typed characters; treat it as "go back to
            # the toggle row" instead.
            if focus == 0:
                self._retreat_step()
            else:
                self._cursors[Step.PROVIDER] = 0
            return
        if event.key == Key.RIGHT or event.key == Key.ENTER:
            # Validate before advancing. Hosted providers need api_key + model.
            if needs_keys and not self.state.provider_api_key.strip():
                self._cursors[Step.PROVIDER] = 1
                self._glow = _ValidationGlow(
                    field="provider_api_key",
                    message=f"api key required for {self.state.provider_kind}",
                    started_at=self.elapsed,
                )
                return
            if needs_keys and not self.state.provider_model.strip():
                self._cursors[Step.PROVIDER] = 2
                self._glow = _ValidationGlow(
                    field="provider_model",
                    message=f"model name required for {self.state.provider_kind}",
                    started_at=self.elapsed,
                )
                return
            self._advance_step()
            return

        # Toggle row: Space cycles type llamacpp → openai → openrouter → llamacpp
        if focus == 0 and event.is_char and event.char == " ":
            cycle = ("llamacpp", "openai", "openrouter")
            try:
                idx = cycle.index(self.state.provider_kind)
            except ValueError:
                idx = 0
            new_kind = cycle[(idx + 1) % len(cycle)]
            # Swap default model when toggling between hosted providers so
            # the user isn't stuck typing every time. Only overwrites the
            # model when the current value matches the OTHER provider's
            # default — preserves user-typed values.
            if new_kind == "openai" and self.state.provider_model in (
                "openai/gpt-oss-20b:free", "",
            ):
                self.state.provider_model = "gpt-4o-mini"
            elif new_kind == "openrouter" and self.state.provider_model in (
                "gpt-4o-mini", "",
            ):
                self.state.provider_model = "openai/gpt-oss-20b:free"
            self.state.provider_kind = new_kind
            return

        # Input rows: only relevant when a hosted provider is selected.
        if not needs_keys:
            return

        if focus == 1:
            # api_key field
            if event.key == Key.BACKSPACE:
                if self.state.provider_api_key:
                    self.state.provider_api_key, _ = delete_prev_grapheme(
                        self.state.provider_api_key,
                        len(self.state.provider_api_key),
                    )
                return
            if event.is_char and event.char and not event.is_ctrl and not event.is_alt:
                for ch in event.char:
                    if ord(ch) >= 0x20 and ch != " ":
                        self.state.provider_api_key += ch
                return

        if focus == 2:
            # model field
            if event.key == Key.BACKSPACE:
                if self.state.provider_model:
                    self.state.provider_model, _ = delete_prev_grapheme(
                        self.state.provider_model,
                        len(self.state.provider_model),
                    )
                return
            if event.is_char and event.char and not event.is_ctrl and not event.is_alt:
                for ch in event.char:
                    # Allow alphanumerics and the few punctuation chars
                    # used in model slugs (/, -, ., :, _).
                    if ord(ch) >= 0x20 and ch != " ":
                        self.state.provider_model += ch
                return

    def _handle_tools(self, event: KeyEvent) -> None:
        """Tools step: space toggles the cursor'd tool on/off.

        Users who just want a chat-only harness can uncheck everything
        and the profile will be saved with an empty tools list. ↑↓
        move the cursor between tools; space (or Enter without a
        modifier key) toggles the currently highlighted one. → advances
        to REVIEW.
        """
        tool_names = tuple(AVAILABLE_TOOLS.keys())
        if not tool_names:
            # No tools registered at all — this step is a no-op. Allow
            # nav but don't try to toggle anything.
            if event.key == Key.ENTER or event.key == Key.RIGHT:
                self._advance_step()
            elif event.key == Key.LEFT:
                self._retreat_step()
            return

        cursor = self._cursors[Step.TOOLS]
        if event.key == Key.UP:
            cursor = (cursor - 1) % len(tool_names)
            self._cursors[Step.TOOLS] = cursor
            return
        if event.key == Key.DOWN:
            cursor = (cursor + 1) % len(tool_names)
            self._cursors[Step.TOOLS] = cursor
            return
        if event.is_char and event.char == " ":
            # Toggle the currently highlighted tool
            current = list(self.state.enabled_tools)
            name = tool_names[cursor]
            if name in current:
                current.remove(name)
            else:
                current.append(name)
            self.state.enabled_tools = tuple(current)
            return
        if event.key == Key.ENTER or event.key == Key.RIGHT:
            self._advance_step()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return

    def _handle_review(self, event: KeyEvent) -> None:
        if event.key == Key.ENTER:
            self._save_and_finish()
            return
        if event.key == Key.LEFT:
            self._retreat_step()
            return
        if event.key == Key.RIGHT:
            self._save_and_finish()
            return

    # ─── Render ───

    def on_tick(self, grid: Grid) -> None:
        """Paint one wizard frame.

        Auto-advance from SAVED to launch the chat after the toast
        has had time to read. The frame loop's stop() unwinds the run
        and run_setup_wizard() then constructs the chat App.
        """
        # Drain any pending bare ESC / partial sequences from the decoder
        for event in self._key_decoder.flush():
            if isinstance(event, KeyEvent):
                self._handle_key(event)

        # SAVED auto-advance check
        if self.current_step == Step.SAVED and self._saved_at is not None:
            if self.elapsed - self._saved_at >= SAVED_AUTO_ADVANCE_S:
                self.stop()

        rows, cols = grid.rows, grid.cols
        if rows < 14 or cols < 60:
            # Too small to render comfortably — show a polite message
            self._paint_too_small(grid, rows, cols)
            return

        # Resolve the wizard's own theme variant for chrome painting.
        # We use the user's currently-selected theme so the wizard
        # paints itself in their preview palette — see-what-you-get.
        chrome_theme = find_theme_or_fallback(self.state.theme_name)
        chrome_variant = chrome_theme.variant(self.state.display_mode)

        # ─── Background ───
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=chrome_variant.bg))

        # ─── Title row ───
        self._paint_title(grid, chrome_variant, cols)

        # ─── Sidebar ───
        sidebar_top = 1
        sidebar_bottom = rows - 1  # leaves room for footer
        self._paint_sidebar(
            grid, chrome_variant,
            x=0, top=sidebar_top, bottom=sidebar_bottom,
        )

        # ─── Main content ───
        main_left = SIDEBAR_W + 1
        main_top = 1
        main_right = cols - 1
        main_bottom = rows - 1
        self._paint_main_content(
            grid, chrome_variant,
            left=main_left, top=main_top,
            right=main_right, bottom=main_bottom,
        )

        # ─── Footer ───
        if rows >= 2:
            self._paint_footer(grid, chrome_variant, y=rows - 1, width=cols)

        # ─── Toast (last so it overlays everything) ───
        self._paint_toast(grid, chrome_variant, cols, rows)

    def _paint_too_small(self, grid: Grid, rows: int, cols: int) -> None:
        """Painted when the terminal is too small to host the full wizard.

        Picks the longest message that fits in the available width so
        even a 30-col window gets a useful hint instead of an empty
        screen. The grid is filled with a neutral bg so any chrome from
        a previous frame is wiped clean.
        """
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=0x000000))
        if rows < 1 or cols < 6:
            return

        candidates = (
            "terminal too small for setup — needs at least 60×14",
            "too small — needs at least 60×14",
            "too small (60x14 min)",
            "too small",
        )
        msg = candidates[-1]
        for candidate in candidates:
            if len(candidate) <= cols:
                msg = candidate
                break

        mx = max(0, (cols - len(msg)) // 2)
        my = max(0, rows // 2)
        paint_text(
            grid, msg, mx, my,
            style=Style(fg=0xCCCCCC, bg=0x000000, attrs=ATTR_BOLD),
        )

    # ─── Title bar ───

    def _paint_title(self, grid: Grid, theme: ThemeVariant, cols: int) -> None:
        title = "successor · setup"
        title_style = Style(fg=theme.fg, bg=theme.bg, attrs=ATTR_BOLD)
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

        # Right-anchored hint about the active profile name (if set)
        if self.state.name:
            hint = f" creating: {self.state.name} "
            hint_style = Style(fg=theme.bg, bg=theme.accent, attrs=ATTR_BOLD)
            hx = max(0, cols - len(hint))
            paint_text(grid, hint, hx, 0, style=hint_style)

    # ─── Sidebar ───

    def _paint_sidebar(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        x: int,
        top: int,
        bottom: int,
    ) -> None:
        """Paint the step list with active spotlight + completed checkmarks."""
        # Sidebar background — slightly elevated bg to distinguish
        sidebar_bg = theme.bg_input
        fill_region(grid, x, top, SIDEBAR_W, bottom - top, style=Style(bg=sidebar_bg))

        # Vertical separator on the right edge of the sidebar
        sep_style = Style(fg=theme.fg_subtle, bg=theme.bg)
        for sy in range(top, bottom):
            grid.set(sy, SIDEBAR_W, Cell("│", sep_style))

        # Step rows
        order = list(Step)
        try:
            current_idx = order.index(self.current_step)
        except ValueError:
            current_idx = 0

        # Compute the gentle pulse for the active step.
        # Base accent color, modulated toward fg over a slow sine.
        pulse_t = 0.5 + 0.5 * (
            __import__("math").sin(self.elapsed * PULSE_HZ * 2 * 3.14159)
        )
        active_fg = lerp_rgb(theme.accent, theme.fg, pulse_t * 0.4)

        row_y = top + 1  # one blank row at the top for breathing room
        for step in _SIDEBAR_STEPS:
            if row_y >= bottom:
                break
            label = _STEP_LABELS[step]
            try:
                step_idx = order.index(step)
            except ValueError:
                continue

            if step_idx < current_idx:
                # Completed step — checkmark
                glyph = "✓"
                glyph_style = Style(fg=theme.accent_warm, bg=sidebar_bg, attrs=ATTR_BOLD)
                label_style = Style(fg=theme.fg_dim, bg=sidebar_bg)
            elif step == self.current_step:
                # Active step — pulsing glyph + bold label
                glyph = "▸"
                glyph_style = Style(fg=active_fg, bg=sidebar_bg, attrs=ATTR_BOLD)
                label_style = Style(fg=active_fg, bg=sidebar_bg, attrs=ATTR_BOLD)
            else:
                # Future step — dim
                glyph = " "
                glyph_style = Style(fg=theme.fg_subtle, bg=sidebar_bg)
                label_style = Style(fg=theme.fg_subtle, bg=sidebar_bg)

            paint_text(grid, f" {glyph} {label}", x, row_y, style=glyph_style)
            # Re-paint just the label portion with label_style so the
            # glyph and label can have different styles
            paint_text(grid, label, x + 3, row_y, style=label_style)
            row_y += 1

    # ─── Main content dispatch ───

    def _paint_main_content(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Dispatch to the active step's painter, with reveal animation."""
        # Section reveal animation: when the user advances to a new
        # step, the content slides up from the bottom over
        # SECTION_REVEAL_S seconds via an ease_out_cubic curve. We
        # implement this as a vertical offset that decays to 0.
        elapsed_in_step = self.elapsed - self._step_entered_at
        if elapsed_in_step < SECTION_REVEAL_S:
            t = ease_out_cubic(elapsed_in_step / SECTION_REVEAL_S)
            slide_offset = int((1.0 - t) * 4)  # max 4 row slide
        else:
            slide_offset = 0

        content_top = top + slide_offset
        if content_top >= bottom - 1:
            return

        # Step heading. SAVED is past the sidebar count so we drop the
        # "step N of M" prefix on it — it's the terminal screen, not
        # a numbered phase.
        if self.current_step == Step.SAVED:
            heading = f" {self._step_title()} "
        else:
            order = list(Step)
            try:
                step_num = order.index(self.current_step) + 1
            except ValueError:
                step_num = 1
            heading = f" step {step_num} of {len(_SIDEBAR_STEPS)} — {self._step_title()} "
        heading_style = Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM)
        paint_text(grid, heading, left + 1, content_top + 1, style=heading_style)

        # Dispatch to the per-step painter
        body_top = content_top + 3
        body_bottom = bottom
        body_left = left + 2
        body_right = right - 1

        if self.current_step == Step.WELCOME:
            self._paint_welcome(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.NAME:
            self._paint_name(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.THEME:
            self._paint_theme(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.MODE:
            self._paint_mode(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.DENSITY:
            self._paint_density(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.INTRO:
            self._paint_intro(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.PROVIDER:
            self._paint_provider(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.TOOLS:
            self._paint_tools(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.COMPACTION:
            self._paint_compaction(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.REVIEW:
            self._paint_review(grid, theme, body_left, body_top, body_right, body_bottom)
        elif self.current_step == Step.SAVED:
            self._paint_saved(grid, theme, body_left, body_top, body_right, body_bottom)

    def _step_title(self) -> str:
        return {
            Step.WELCOME: "welcome",
            Step.NAME: "give your profile a name",
            Step.THEME: "choose a color theme",
            Step.MODE: "choose dark or light",
            Step.DENSITY: "choose layout density",
            Step.INTRO: "intro animation",
            Step.PROVIDER: "model provider",
            Step.TOOLS: "enable tools",
            Step.COMPACTION: "autocompact behavior",
            Step.REVIEW: "review and save",
            Step.SAVED: "saved",
        }[self.current_step]

    # ─── Per-step painters ───

    def _paint_welcome(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Welcome screen: centered braille frame + typewriter intro text.

        Uses the existing BrailleArt resampling so the frame fits the
        available space at any terminal size. The text fades in via
        a typewriter effect over ~1.5 seconds.
        """
        avail_w = right - left
        avail_h = bottom - top
        if avail_w < 30 or avail_h < 6:
            return

        # Braille frame at the top — use ~half the available height
        frame_h = max(0, min(avail_h - 6, 12))
        if self._welcome_frame is not None and frame_h >= 4:
            # Pick a frame size that fits horizontally too. The frame
            # is 28×30 dots in source — 14 cells wide × ~7 cells tall
            # at native, scales up cleanly.
            target_h = frame_h
            target_w = min(avail_w - 4, target_h * 2)
            if target_w >= 4 and target_h >= 4:
                lines = self._welcome_frame.layout(target_w, target_h)
                fy = top
                fx = left + max(0, (avail_w - target_w) // 2)
                frame_style = Style(fg=theme.accent, bg=theme.bg)
                for i, line in enumerate(lines):
                    if fy + i >= bottom:
                        break
                    paint_text(grid, line, fx, fy + i, style=frame_style)

        # Typewriter intro text below the frame
        text_y = top + frame_h + 1
        if text_y >= bottom:
            return

        intro_lines = [
            "let's create a chat profile",
            "",
            "a profile bundles your visual style, system prompt,",
            "and provider into one named persona — switch personas",
            "with /profile or Ctrl+P inside the chat",
        ]

        if self._welcome_started_at == 0.0:
            self._welcome_started_at = self.elapsed
        elapsed = max(0.0, self.elapsed - self._welcome_started_at)
        # How many characters total should be visible by now?
        # Joined length of all lines is the budget; we count chars
        # incrementally and stop when we run out.
        total_chars = sum(len(l) for l in intro_lines)
        visible_chars = min(total_chars, int(elapsed * WELCOME_TYPEWRITER_CPS))

        cursor = 0
        for i, line in enumerate(intro_lines):
            ly = text_y + i
            if ly >= bottom:
                break
            # How many chars of this line should be visible?
            line_chars = max(0, min(len(line), visible_chars - cursor))
            if line_chars <= 0 and cursor >= visible_chars:
                continue
            visible_text = line[:line_chars]
            tx = left + max(0, (avail_w - len(line)) // 2)
            line_style = Style(
                fg=theme.fg if i == 0 else theme.fg_dim,
                bg=theme.bg,
                attrs=ATTR_BOLD if i == 0 else 0,
            )
            paint_text(grid, visible_text, tx, ly, style=line_style)
            cursor += len(line)

        # Hint at the bottom — fades in after the typewriter completes
        hint_y = bottom - 2
        if hint_y > text_y + len(intro_lines) and visible_chars >= total_chars:
            hint = "press → or Enter to begin · Esc to cancel"
            fade_t = min(1.0, (elapsed - total_chars / WELCOME_TYPEWRITER_CPS) * 2.5)
            hint_color = lerp_rgb(theme.bg, theme.accent_warm, fade_t)
            hx = left + max(0, (avail_w - len(hint)) // 2)
            paint_text(
                grid, hint, hx, hint_y,
                style=Style(fg=hint_color, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
            )

    def _paint_name(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Name input field with cursor blink and validation glow."""
        # Prompt text
        prompt = "what's the name for your new profile?"
        paint_text(
            grid, prompt, left, top,
            style=Style(fg=theme.fg, bg=theme.bg),
        )

        # Helper text
        helper = "letters, numbers, dash, underscore — no spaces"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        # Input field box
        field_y = top + 3
        field_w = min(right - left, MAX_NAME_LEN + 6)

        # Validation glow color — fades from a warning red back to the
        # normal border over VALIDATION_GLOW_S seconds
        if self._glow is not None and self._glow.field == "name":
            elapsed_glow = self.elapsed - self._glow.started_at
            if elapsed_glow >= VALIDATION_GLOW_S:
                self._glow = None
                border_color = theme.accent
            else:
                t = elapsed_glow / VALIDATION_GLOW_S
                border_color = lerp_rgb(theme.accent_warn, theme.accent, t)
        else:
            border_color = theme.accent

        border_style = Style(fg=border_color, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_box(
            grid, left, field_y, field_w, 3,
            style=border_style, fill_style=fill_style, chars=BOX_ROUND,
        )

        # Field content
        cx = left + 2
        cy = field_y + 1
        field_text = self.state.name
        paint_text(
            grid, field_text, cx, cy,
            style=Style(fg=theme.fg, bg=theme.bg_input, attrs=ATTR_BOLD),
        )

        # Cursor blink at end of input
        cursor_x = cx + len(field_text)
        if cursor_x < left + field_w - 1:
            visible = (int(self.elapsed * 3) % 2) == 0
            if visible:
                grid.set(
                    cy, cursor_x,
                    Cell(" ", Style(fg=theme.bg_input, bg=theme.fg)),
                )

        # Validation message under the field, if active
        if self._glow is not None and self._glow.field == "name":
            msg_y = field_y + 4
            if msg_y < bottom:
                paint_text(
                    grid, f"⚠ {self._glow.message}", left, msg_y,
                    style=Style(fg=theme.accent_warn, bg=theme.bg, attrs=ATTR_BOLD),
                )

        # Live preview is hidden on the name step — there's nothing
        # theme-related to preview yet, and the input field gets the
        # full attention of the step.

    def _paint_theme(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Theme picker with live preview pane on the right side."""
        # Layout: theme list on the left, live preview on the right
        # Split the available width so the preview gets ~60% on wide
        # terminals, ~50% on narrow ones
        avail_w = right - left
        if avail_w >= 90:
            list_w = 32
        elif avail_w >= 70:
            list_w = 26
        else:
            list_w = max(18, avail_w // 2)
        preview_x = left + list_w + 2

        # Theme list
        themes = all_themes()
        if not themes:
            paint_text(
                grid, "(no themes loaded — drop *.json into ~/.config/successor/themes/)",
                left, top,
                style=Style(fg=theme.fg_dim, bg=theme.bg),
            )
            return

        cursor = self._cursors[Step.THEME]
        for i, t in enumerate(themes):
            row_y = top + i
            if row_y >= bottom:
                break
            is_selected = i == cursor
            glyph = "▸" if is_selected else " "
            line_style = Style(
                fg=theme.accent if is_selected else theme.fg,
                bg=theme.bg,
                attrs=ATTR_BOLD if is_selected else 0,
            )
            label = f"{glyph} {t.icon}  {t.name}"
            paint_text(grid, label, left, row_y, style=line_style)

        # Description of the currently-selected theme
        desc_y = top + len(themes) + 1
        if desc_y < bottom:
            current = themes[cursor]
            desc = current.description or "(no description)"
            # Soft-wrap the description to list_w columns
            words = desc.split()
            line = ""
            wy = desc_y
            for word in words:
                if len(line) + len(word) + 1 > list_w:
                    if wy < bottom:
                        paint_text(
                            grid, line, left, wy,
                            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_ITALIC),
                        )
                    wy += 1
                    line = word
                else:
                    line = f"{line} {word}".strip()
            if line and wy < bottom:
                paint_text(
                    grid, line, left, wy,
                    style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_ITALIC),
                )

        # ─── Live preview pane ───
        self._paint_live_preview(
            grid, theme,
            x=preview_x, y=top, w=right - preview_x, h=bottom - top,
        )

    def _paint_mode(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Display mode toggle (dark/light) with live preview."""
        avail_w = right - left
        list_w = max(18, min(28, avail_w // 3))
        preview_x = left + list_w + 2

        prompt = "dark or light?"
        paint_text(
            grid, prompt, left, top,
            style=Style(fg=theme.fg, bg=theme.bg),
        )

        helper = "↑↓ or space to toggle"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        cursor = self._cursors[Step.MODE]
        for i, mode in enumerate(_MODE_OPTIONS):
            row_y = top + 3 + i
            if row_y >= bottom:
                break
            is_selected = i == cursor
            glyph = "▸" if is_selected else " "
            icon = "☾" if mode == "dark" else "☀"
            label = f"{glyph} {icon}  {mode}"
            line_style = Style(
                fg=theme.accent if is_selected else theme.fg,
                bg=theme.bg,
                attrs=ATTR_BOLD if is_selected else 0,
            )
            paint_text(grid, label, left, row_y, style=line_style)

        self._paint_live_preview(
            grid, theme,
            x=preview_x, y=top, w=right - preview_x, h=bottom - top,
        )

    def _paint_density(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Density picker with live preview."""
        avail_w = right - left
        list_w = max(18, min(30, avail_w // 3))
        preview_x = left + list_w + 2

        prompt = "how dense should the chat layout be?"
        paint_text(grid, prompt, left, top, style=Style(fg=theme.fg, bg=theme.bg))
        helper = "compact = max info, spacious = lots of breathing room"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        cursor = self._cursors[Step.DENSITY]
        for i, density in enumerate(_DENSITY_OPTIONS):
            row_y = top + 3 + i
            if row_y >= bottom:
                break
            is_selected = i == cursor
            glyph = "▸" if is_selected else " "
            label = f"{glyph}  {density}"
            line_style = Style(
                fg=theme.accent if is_selected else theme.fg,
                bg=theme.bg,
                attrs=ATTR_BOLD if is_selected else 0,
            )
            paint_text(grid, label, left, row_y, style=line_style)

        self._paint_live_preview(
            grid, theme,
            x=preview_x, y=top, w=right - preview_x, h=bottom - top,
        )

    def _paint_intro(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Intro animation toggle (none / successor emergence)."""
        prompt = "play an intro animation when the chat opens?"
        paint_text(grid, prompt, left, top, style=Style(fg=theme.fg, bg=theme.bg))
        helper = "the successor emergence portrait takes ~5s, any key skips"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        cursor = self._cursors[Step.INTRO]
        for i, (value, label) in enumerate(_INTRO_OPTIONS):
            row_y = top + 3 + i
            if row_y >= bottom:
                break
            is_selected = i == cursor
            glyph = "▸" if is_selected else " "
            line = f"{glyph}  {label}"
            line_style = Style(
                fg=theme.accent if is_selected else theme.fg,
                bg=theme.bg,
                attrs=ATTR_BOLD if is_selected else 0,
            )
            paint_text(grid, line, left, row_y, style=line_style)

    def _paint_provider(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Provider step painter.

        Layout:
            which model provider should this profile talk to?
            ↑↓ move · space toggles type · → next

            ▸ [▣] local llama.cpp     uses http://localhost:8080
              [ ] openai                api.openai.com — api key required
              [ ] openrouter            openrouter.ai — api key required

            api_key  ••••••••••••      (only for hosted providers)
            model    gpt-4o-mini

        The active row is highlighted with a ▸ glyph and accent color.
        Inputs render in code-tinted boxes so they're visually distinct
        from selectable rows. The api_key field renders as bullets
        unless the cursor is on it (then plaintext while editing).
        """
        prompt = "which model provider should this profile talk to?"
        paint_text(grid, prompt, left, top, style=Style(fg=theme.fg, bg=theme.bg))
        helper = "↑↓ move · space toggles type · → next"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        focus = self._cursors[Step.PROVIDER]
        needs_keys = self.state.provider_kind in ("openrouter", "openai")

        # Toggle row group (focus = 0 — single focusable row, but we
        # paint all three options stacked so the user can see what's
        # available even before they hit Space).
        toggle_y = top + 3
        sel_glyph = "▸" if focus == 0 else " "
        provider_options = (
            ("llamacpp", "local llama.cpp", "free + private, needs llama-server running"),
            ("openai", "openai", "pay-per-use against your OpenAI credits"),
            ("openrouter", "openrouter", "free models available, no card needed"),
        )
        for i, (kind, label, hint) in enumerate(provider_options):
            row_y = toggle_y + i
            if row_y >= bottom:
                break
            is_picked = self.state.provider_kind == kind
            check = "[▣]" if is_picked else "[ ]"
            cursor_glyph = sel_glyph if i == 0 else " "
            line = f"{cursor_glyph} {check}  {label}"
            line_style = Style(
                fg=theme.accent if (focus == 0 and is_picked) else theme.fg,
                bg=theme.bg,
                attrs=ATTR_BOLD if is_picked else 0,
            )
            paint_text(grid, line, left, row_y, style=line_style)
            paint_text(
                grid, hint, left + len(line) + 4, row_y,
                style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
            )

        # Input rows for hosted providers (focus = 1, 2)
        if not needs_keys:
            # llamacpp picked — leave the rest blank but show one helpful line
            note_y = toggle_y + 5
            if note_y < bottom:
                paint_text(
                    grid,
                    "press → to continue. nothing else to configure.",
                    left, note_y,
                    style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
                )
            return

        # api_key field
        api_y = toggle_y + 4
        if api_y < bottom:
            label = "api_key"
            label_w = max(8, len(label))
            label_style = Style(
                fg=theme.accent if focus == 1 else theme.fg_dim,
                bg=theme.bg,
                attrs=ATTR_BOLD if focus == 1 else ATTR_DIM,
            )
            cursor_glyph = "▸" if focus == 1 else " "
            paint_text(grid, cursor_glyph, left, api_y, style=label_style)
            paint_text(grid, label.rjust(label_w), left + 2, api_y, style=label_style)

            # Render the value: bullets when not focused, plaintext when focused
            value = self.state.provider_api_key
            if focus == 1:
                display = value
            else:
                display = "•" * min(len(value), 24) if value else "(unset)"
            value_x = left + 2 + label_w + 2
            value_style = Style(
                fg=theme.fg if focus == 1 else theme.fg_dim,
                bg=theme.bg_input,
            )
            avail_w = max(0, right - value_x - 2)
            display = display[:avail_w] if avail_w > 0 else ""
            # Pad to a fixed width so the input box has a visible extent
            box_w = max(40, len(display) + 4)
            box_w = min(box_w, avail_w)
            if box_w > 0:
                fill_region(grid, value_x, api_y, box_w, 1, style=Style(bg=theme.bg_input))
                paint_text(grid, display, value_x + 1, api_y, style=value_style)
                # Cursor shows when this row is focused
                if focus == 1:
                    cur_x = value_x + 1 + len(display)
                    if cur_x < value_x + box_w - 1:
                        cursor_visible = (int(self.elapsed * 2) % 2) == 0
                        if cursor_visible:
                            grid.set(api_y, cur_x, Cell(" ", Style(fg=theme.bg_input, bg=theme.fg)))

        # model field
        model_y = api_y + 1
        if model_y < bottom:
            label = "model"
            label_w = max(8, len("api_key"))
            label_style = Style(
                fg=theme.accent if focus == 2 else theme.fg_dim,
                bg=theme.bg,
                attrs=ATTR_BOLD if focus == 2 else ATTR_DIM,
            )
            cursor_glyph = "▸" if focus == 2 else " "
            paint_text(grid, cursor_glyph, left, model_y, style=label_style)
            paint_text(grid, label.rjust(label_w), left + 2, model_y, style=label_style)

            value = self.state.provider_model or "(unset)"
            value_x = left + 2 + label_w + 2
            value_style = Style(
                fg=theme.fg if focus == 2 else theme.fg_dim,
                bg=theme.bg_input,
            )
            avail_w = max(0, right - value_x - 2)
            display = value[:avail_w] if avail_w > 0 else ""
            box_w = max(40, len(display) + 4)
            box_w = min(box_w, avail_w)
            if box_w > 0:
                fill_region(grid, value_x, model_y, box_w, 1, style=Style(bg=theme.bg_input))
                paint_text(grid, display, value_x + 1, model_y, style=value_style)
                if focus == 2:
                    cur_x = value_x + 1 + len(display)
                    if cur_x < value_x + box_w - 1:
                        cursor_visible = (int(self.elapsed * 2) % 2) == 0
                        if cursor_visible:
                            grid.set(model_y, cur_x, Cell(" ", Style(fg=theme.bg_input, bg=theme.fg)))

        # Hint footer
        hint_y = model_y + 2
        if hint_y < bottom:
            if self.state.provider_api_key.strip() and self.state.provider_model.strip():
                hint = "press → to continue"
                color = theme.accent
            else:
                hint = "fill api_key and model, then press →"
                color = theme.accent_warm
            paint_text(
                grid, hint, left, hint_y,
                style=Style(fg=color, bg=theme.bg, attrs=ATTR_BOLD),
            )

    def _paint_tools(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Tool picker — checkboxes for each known tool in the registry.

        Users who want a chat-only harness can uncheck everything and
        save a profile with no tools. Users who want a richer setup
        can enable bash (and future tools as they're added). The step
        is the only place in the wizard where the user decides what
        the harness is *allowed* to do on their behalf.
        """
        prompt = "what should this profile be allowed to do?"
        paint_text(grid, prompt, left, top, style=Style(fg=theme.fg, bg=theme.bg))
        helper = "↑↓ move · space toggles · → next (chat-only is fine — uncheck everything)"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        tool_names = tuple(AVAILABLE_TOOLS.keys())
        if not tool_names:
            paint_text(
                grid, "(no tools registered)", left, top + 3,
                style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_ITALIC),
            )
            return

        cursor = self._cursors[Step.TOOLS]
        enabled = set(self.state.enabled_tools)

        for i, name in enumerate(tool_names):
            row_y = top + 3 + i * 2
            if row_y >= bottom - 1:
                break
            descriptor = AVAILABLE_TOOLS[name]
            is_cursor = i == cursor
            is_on = name in enabled
            glyph = "▸" if is_cursor else " "
            check = "[✓]" if is_on else "[ ]"
            row_fg = theme.accent if is_cursor else theme.fg
            row_attrs = ATTR_BOLD if is_cursor else 0
            label = f"{glyph} {check}  {descriptor.label}"
            paint_text(
                grid, label, left, row_y,
                style=Style(fg=row_fg, bg=theme.bg, attrs=row_attrs),
            )
            # Description line underneath, dimmed
            desc = descriptor.description
            if row_y + 1 < bottom:
                # Indent under the checkbox for visual alignment
                paint_text(
                    grid, desc, left + 7, row_y + 1,
                    style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
                )

        # Summary footer — how many tools are enabled
        count = len(enabled)
        summary_y = bottom - 2
        if summary_y > top + 3 + len(tool_names) * 2:
            if count == 0:
                summary = "chat-only mode — no tools enabled"
                summary_color = theme.fg_dim
            elif count == 1:
                summary = "1 tool enabled"
                summary_color = theme.accent
            else:
                summary = f"{count} tools enabled"
                summary_color = theme.accent
            paint_text(
                grid, summary, left, summary_y,
                style=Style(fg=summary_color, bg=theme.bg, attrs=ATTR_BOLD),
            )

    def _paint_compaction(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Compaction preset picker.

        Lists the four presets (default, aggressive, lazy, off) with
        a one-line description of each. Below the list, a small "live
        preview" panel shows the resolved buffer thresholds the
        currently-selected preset would produce against a 200K window
        — gives the user a concrete feel for what they're picking.
        """
        prompt = "when should the harness compact your conversation?"
        paint_text(grid, prompt, left, top, style=Style(fg=theme.fg, bg=theme.bg))
        helper = "↑↓ or space to cycle · → to confirm · the chat's /budget command shows live thresholds"
        paint_text(
            grid, helper, left, top + 1,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
        )

        presets = _compaction_presets()
        cursor = self._cursors[Step.COMPACTION]

        # Each preset gets two rows: the label, then the description.
        list_top = top + 3
        for i, (key, label, desc, _cfg) in enumerate(presets):
            row_y = list_top + i * 3
            if row_y >= bottom - 1:
                break
            is_selected = i == cursor
            glyph = "▸" if is_selected else " "
            line = f"{glyph}  {label}"
            line_style = Style(
                fg=theme.accent if is_selected else theme.fg,
                bg=theme.bg,
                attrs=ATTR_BOLD if is_selected else 0,
            )
            paint_text(grid, line, left, row_y, style=line_style)
            if row_y + 1 < bottom:
                desc_style = Style(
                    fg=theme.fg_dim if is_selected else theme.fg_subtle,
                    bg=theme.bg,
                    attrs=ATTR_ITALIC if is_selected else ATTR_DIM | ATTR_ITALIC,
                )
                paint_text(grid, f"   {desc}", left, row_y + 1, style=desc_style)

        # Live preview panel — sits at the bottom of the body, shows
        # what the selected preset's buffers would look like against
        # a representative 200K window.
        if cursor < len(presets):
            _key, _label, _desc, cfg = presets[cursor]
            preview_y = list_top + len(presets) * 3 + 1
            if preview_y < bottom - 2:
                self._paint_compaction_preview(
                    grid, theme, cfg, left, preview_y, right - left,
                )

    def _paint_compaction_preview(
        self,
        grid: Grid,
        theme: ThemeVariant,
        cfg,            # CompactionConfig
        x: int,
        y: int,
        w: int,
    ) -> None:
        """Render the resolved-buffer preview for a CompactionConfig.

        Shows three lines computed against a 200K reference window:
            warning fires at      X tokens (Y%)
            autocompact fires at  X tokens (Y%)
            blocking refusal at   X tokens (Y%)

        This gives the user a concrete sense of what each preset
        does without making them do the math.
        """
        REF_WINDOW = 200_000
        warning_buf, autocompact_buf, blocking_buf = cfg.buffers_for_window(REF_WINDOW)
        warning_at = REF_WINDOW - warning_buf
        autocompact_at = REF_WINDOW - autocompact_buf
        blocking_at = REF_WINDOW - blocking_buf

        title = f"on a {REF_WINDOW // 1000}K context window:"
        paint_text(
            grid, title, x, y,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM),
        )

        if not cfg.enabled:
            line = "  autocompact disabled — only blocking refusal applies"
            paint_text(
                grid, line, x, y + 1,
                style=Style(fg=theme.accent_warm, bg=theme.bg, attrs=ATTR_ITALIC),
            )
            line2 = f"  blocking refusal at {blocking_at:,} tokens"
            paint_text(
                grid, line2, x, y + 2,
                style=Style(fg=theme.fg_dim, bg=theme.bg),
            )
            return

        warning_pct = 100 * warning_at / REF_WINDOW
        auto_pct = 100 * autocompact_at / REF_WINDOW
        block_pct = 100 * blocking_at / REF_WINDOW
        rows = [
            ("warning at",     warning_at, warning_pct),
            ("autocompact at", autocompact_at, auto_pct),
            ("blocking at",    blocking_at, block_pct),
        ]
        for i, (label, val, pct) in enumerate(rows):
            line = f"  {label:<16} {val:>9,} tokens  ({pct:.1f}% full)"
            paint_text(
                grid, line, x, y + 1 + i,
                style=Style(fg=theme.fg_dim, bg=theme.bg),
            )

    def _paint_review(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Final review showing the profile JSON before saving."""
        prompt = f"ready to save '{self.state.name}'?"
        paint_text(
            grid, prompt, left, top,
            style=Style(fg=theme.fg, bg=theme.bg, attrs=ATTR_BOLD),
        )

        # Field summary in two columns
        if self.state.enabled_tools:
            tools_label = ", ".join(self.state.enabled_tools)
        else:
            tools_label = "(none — chat-only)"
        if self.state.provider_kind == "openrouter":
            provider_summary = f"openrouter · {self.state.provider_model}"
        elif self.state.provider_kind == "openai":
            provider_summary = f"openai · {self.state.provider_model}"
        else:
            provider_summary = "local llama.cpp at http://localhost:8080"
        rows_data = [
            ("name", self.state.name),
            ("theme", self.state.theme_name),
            ("display mode", self.state.display_mode),
            ("density", self.state.density),
            ("intro animation", self.state.intro_animation or "(none)"),
            ("system prompt", "default — edit JSON file to customize"),
            ("provider", provider_summary),
            ("skills", "(none — phase 5 not yet wired)"),
            ("tools", tools_label),
        ]
        label_w = max(len(label) for label, _ in rows_data)
        for i, (label, value) in enumerate(rows_data):
            row_y = top + 2 + i
            if row_y >= bottom - 3:
                break
            paint_text(
                grid, label.rjust(label_w), left, row_y,
                style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM),
            )
            paint_text(
                grid, "  ", left + label_w, row_y,
                style=Style(bg=theme.bg),
            )
            paint_text(
                grid, str(value), left + label_w + 2, row_y,
                style=Style(fg=theme.fg, bg=theme.bg),
            )

        # Save hint at the bottom
        hint_y = bottom - 2
        if hint_y > top + len(rows_data) + 2:
            hint = "press Enter to save · ← to back · Esc to cancel"
            paint_text(
                grid, hint, left, hint_y,
                style=Style(fg=theme.accent_warm, bg=theme.bg, attrs=ATTR_BOLD),
            )

    def _paint_saved(
        self,
        grid: Grid,
        theme: ThemeVariant,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> None:
        """Saved screen — auto-advances into the chat. Just shows the toast."""
        msg = f"profile '{self.state.name}' saved"
        avail_w = right - left
        mx = left + max(0, (avail_w - len(msg)) // 2)
        my = top + max(0, (bottom - top) // 2)
        paint_text(
            grid, msg, mx, my,
            style=Style(fg=theme.accent, bg=theme.bg, attrs=ATTR_BOLD),
        )
        sub = "opening chat with your new profile…"
        sx = left + max(0, (avail_w - len(sub)) // 2)
        if my + 2 < bottom:
            paint_text(
                grid, sub, sx, my + 2,
                style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC),
            )

    # ─── Live preview pane ───

    def _paint_live_preview(
        self,
        grid: Grid,
        theme: ThemeVariant,
        *,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        """Render the preview chat into a sub-grid and copy cells in.

        This is the centerpiece capability — the preview is a real
        SuccessorChat that we mutate as the user picks options. The chat's
        existing on_tick paints into a Grid we pass it; we then walk
        the cells and copy them into our own grid at (x, y).

        Pretext-shaped efficiency: the preview chat caches its
        PreparedMarkdown wraps per width, so re-tick at the same width
        is essentially free. Re-rendering 30fps with no recomputation.
        """
        if w < 30 or h < 8:
            return

        # Header label above the preview
        if h >= 1:
            label = "  live preview ─" + "─" * max(0, w - 18)
            paint_text(
                grid, label, x, y,
                style=Style(fg=theme.fg_subtle, bg=theme.bg, attrs=ATTR_DIM),
            )

        preview_y = y + 1
        preview_h = h - 1
        if preview_h < 6:
            return

        # Build a sub-grid sized for the preview pane and tick the
        # preview chat into it.
        sub = Grid(preview_h, w)
        try:
            self._preview_chat.on_tick(sub)
        except Exception:
            # If the preview chat crashes for any reason, just bail —
            # the wizard's own chrome paints fine without the preview
            return

        # Copy cells from the sub-grid into the main grid at (x, preview_y).
        # O(rows*cols) per copy — at 30fps with a 30x80 preview, that's
        # ~72k cells/sec which is well within the renderer's budget.
        for sy in range(sub.rows):
            dst_y = preview_y + sy
            if dst_y >= grid.rows:
                break
            for sx in range(sub.cols):
                dst_x = x + sx
                if dst_x >= grid.cols:
                    break
                cell = sub.at(sy, sx)
                # Skip wide-tail cells when copying so the diff layer
                # doesn't double-paint
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

        # Left side: keybinds for the current step
        keybinds = self._footer_keybinds()
        paint_text(
            grid, keybinds, 1, y,
            style=Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_DIM),
        )

        # Right side: step progress bar with color lerp
        order = list(Step)
        try:
            current_idx = order.index(self.current_step)
        except ValueError:
            current_idx = 0
        total = len(_SIDEBAR_STEPS)
        # SAVED is past the sidebar steps — clamp the visible counter
        # to total/total so the footer reads "step 7/7" with a full bar.
        if self.current_step == Step.SAVED:
            display_num = total
            pct = 1.0
        else:
            display_num = current_idx + 1
            pct = (current_idx + 1) / total

        # Color lerp same as the chat ctx bar
        if pct < 0.6:
            bar_fg = lerp_rgb(theme.accent, theme.accent_warm, pct / 0.6)
        elif pct < 0.85:
            bar_fg = lerp_rgb(theme.accent_warm, theme.accent_warn, (pct - 0.6) / 0.25)
        else:
            bar_fg = theme.accent_warn

        progress_label = f" step {display_num}/{total} "
        bar_w = 12
        right_text = progress_label + "▆" * int(round(pct * bar_w)) + "░" * (
            bar_w - int(round(pct * bar_w))
        )
        right_x = max(len(keybinds) + 2, width - len(right_text) - 1)
        paint_text(
            grid, progress_label, right_x, y,
            style=Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_DIM),
        )
        bar_x = right_x + len(progress_label)
        filled = int(round(pct * bar_w))
        if filled > 0:
            paint_text(
                grid, "▆" * filled, bar_x, y,
                style=Style(fg=bar_fg, bg=theme.bg_footer, attrs=ATTR_BOLD),
            )
        if bar_w - filled > 0:
            paint_text(
                grid, "░" * (bar_w - filled), bar_x + filled, y,
                style=Style(fg=theme.fg_subtle, bg=theme.bg_footer),
            )

    def _footer_keybinds(self) -> str:
        if self.current_step == Step.WELCOME:
            return "→ begin · esc cancel"
        if self.current_step == Step.NAME:
            return "type name · ⏎ next · ← back · esc cancel"
        if self.current_step in (Step.THEME, Step.DENSITY):
            return "↑↓ choose · ⏎ next · ← back · esc cancel"
        if self.current_step in (Step.MODE, Step.INTRO):
            return "↑↓ or space toggle · ⏎ next · ← back · esc cancel"
        if self.current_step == Step.REVIEW:
            return "⏎ save · ← back · esc cancel"
        if self.current_step == Step.SAVED:
            return "opening chat…"
        return ""

    # ─── Toast ───

    def _paint_toast(
        self,
        grid: Grid,
        theme: ThemeVariant,
        cols: int,
        rows: int,
    ) -> None:
        """Paint the active toast notification, if any.

        Slides in from the top-right edge over 200ms, holds for the
        rest of TOAST_DURATION_S, then fades out over the last 500ms.
        Cleared automatically when its lifetime expires.
        """
        if self._toast is None:
            return
        elapsed = self.elapsed - self._toast.started_at
        if elapsed >= TOAST_DURATION_S:
            self._toast = None
            return

        text = f"  ✓ {self._toast.text}  "
        text_w = len(text)

        # Slide-in animation: x position starts off-screen right and
        # eases into final position over 200ms
        slide_in_s = 0.2
        if elapsed < slide_in_s:
            t = ease_out_cubic(elapsed / slide_in_s)
            offscreen_x = cols
            target_x = cols - text_w - 2
            x = int(offscreen_x + (target_x - offscreen_x) * t)
        else:
            x = cols - text_w - 2

        # Fade-out animation: alpha lerps from 1 to 0 over the last 500ms
        fade_out_s = 0.5
        time_until_end = TOAST_DURATION_S - elapsed
        if time_until_end < fade_out_s:
            fade_t = time_until_end / fade_out_s
            text_color = lerp_rgb(theme.bg_footer, theme.fg, fade_t)
            bg_color = lerp_rgb(theme.bg, theme.accent, fade_t)
        else:
            text_color = theme.fg
            bg_color = theme.accent

        y = 1
        if y >= rows or x < 0:
            return
        paint_text(
            grid, text, x, y,
            style=Style(fg=text_color, bg=bg_color, attrs=ATTR_BOLD),
        )


# ─── Public entry point ───


def run_setup_wizard() -> Profile | None:
    """Run the setup wizard interactively. Returns the saved Profile or None.

    On success: returns the Profile object that was just saved. The
    caller is responsible for launching the chat with that profile.

    On cancel (Esc / Ctrl+C / save failed): returns None.
    """
    wizard = SuccessorSetup()
    wizard.run()
    if wizard.should_launch_chat and wizard.saved_profile is not None:
        return wizard.saved_profile
    return None
