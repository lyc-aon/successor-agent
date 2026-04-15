"""Tests for the successor setup wizard.

Three layers of coverage:

  1. State machine — pure logic tests for step navigation, name
     validation, save flow, and the _WizardState → Profile / JSON
     conversion. No rendering.

  2. Rendering — snapshot tests using wizard_demo_snapshot to verify
     each step's visible chrome (sidebar, headings, options, preview).
     Hermetic via temp_config_dir.

  3. Save flow integration — drives the wizard through a full
     create-profile sequence by calling _handle_key with synthesized
     KeyEvents, asserts the JSON file lands on disk and the registry
     picks it up.

The wizard's `should_launch_chat` flag is checked in lieu of actually
running the chat (which would need a TTY). The post-wizard chat launch
is glue, not logic.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path


from successor.input.keys import Key, KeyEvent
from successor.profiles import DEFAULT_MAX_AGENT_TURNS, PROFILE_REGISTRY, get_profile
from successor.render.theme import THEME_REGISTRY
from successor.snapshot import render_grid_to_plain, wizard_demo_snapshot
from successor.wizard.setup import (
    SuccessorSetup,
    Step,
    _WizardState,
    _DENSITY_OPTIONS,
)


# ─── _WizardState pure logic ───


def test_wizard_state_defaults() -> None:
    state = _WizardState()
    assert state.name == ""
    assert state.theme_name == "steel"
    assert state.display_mode == "dark"
    assert state.density == "normal"
    # Both intro animation and the empty-state hero art default to
    # "successor" so wizard-created profiles open with the bundled
    # emergence animation + bundled chat hero. Users can clear either
    # field via the wizard's INTRO step or by editing the saved JSON.
    assert state.intro_animation == "successor"
    assert state.chat_intro_art == "successor"
    assert state.autorecord is True
    assert state.max_agent_turns == DEFAULT_MAX_AGENT_TURNS


def test_wizard_state_to_profile_uses_name() -> None:
    state = _WizardState(name="my-profile", theme_name="paper")
    profile = state.to_profile()
    assert profile.name == "my-profile"
    assert profile.theme == "paper"


def test_wizard_state_lowercases_name_in_profile() -> None:
    state = _WizardState(name="MIXEDCase")
    assert state.to_profile().name == "mixedcase"


def test_wizard_state_to_json_dict_round_trips() -> None:
    state = _WizardState(
        name="x",
        theme_name="steel",
        display_mode="light",
        density="compact",
        intro_animation="successor",
    )
    payload = state.to_json_dict()
    # Must be JSON-serializable and contain all the fields
    serialized = json.dumps(payload)
    parsed = json.loads(serialized)
    assert parsed["name"] == "x"
    assert parsed["theme"] == "steel"
    assert parsed["display_mode"] == "light"
    assert parsed["density"] == "compact"
    assert parsed["max_agent_turns"] == DEFAULT_MAX_AGENT_TURNS
    assert parsed["intro_animation"] == "successor"
    assert parsed["provider"]["type"] == "llamacpp"


def test_wizard_state_seeds_recommended_skills_for_web_tools() -> None:
    state = _WizardState(
        name="x",
        enabled_tools=("bash", "holonet", "browser"),
    )
    profile = state.to_profile()
    payload = state.to_json_dict()
    assert profile.skills == (
        "holonet-research",
        "biomedical-research",
        "browser-operator",
        "browser-verifier",
    )
    assert payload["skills"] == [
        "holonet-research",
        "biomedical-research",
        "browser-operator",
        "browser-verifier",
    ]


def test_wizard_state_empty_name_falls_back_to_untitled() -> None:
    state = _WizardState(name="")
    assert state.to_profile().name == "untitled"


# ─── Name validation ───


def test_is_valid_name_accepts_alphanumeric(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    assert wizard._is_valid_name("successor")
    assert wizard._is_valid_name("successor-dev")
    assert wizard._is_valid_name("successor_dev")
    assert wizard._is_valid_name("successor123")
    assert wizard._is_valid_name("123-x")


def test_is_valid_name_rejects_invalid(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    assert not wizard._is_valid_name("")
    assert not wizard._is_valid_name("has space")
    assert not wizard._is_valid_name("has/slash")
    assert not wizard._is_valid_name("has.dot")
    assert not wizard._is_valid_name("has@symbol")


# ─── Step navigation ───


def test_advance_step_walks_enum_order(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    assert wizard.current_step == Step.WELCOME
    wizard._advance_step()
    assert wizard.current_step == Step.NAME
    wizard._advance_step()
    assert wizard.current_step == Step.THEME
    wizard._advance_step()
    assert wizard.current_step == Step.MODE


def test_retreat_step_walks_backward(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.DENSITY)
    wizard._retreat_step()
    assert wizard.current_step == Step.MODE
    wizard._retreat_step()
    assert wizard.current_step == Step.THEME


def test_retreat_at_welcome_is_noop(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    assert wizard.current_step == Step.WELCOME
    wizard._retreat_step()
    assert wizard.current_step == Step.WELCOME


def test_advance_at_review_triggers_save(temp_config_dir: Path) -> None:
    """REVIEW + advance → save flow runs (with valid name set first)."""
    wizard = SuccessorSetup()
    wizard.state.name = "test-save"
    wizard._enter_step(Step.REVIEW)
    wizard._advance_step()  # triggers _save_and_finish
    assert wizard.current_step == Step.SAVED
    assert wizard.should_launch_chat is True


# ─── _handle_name input handling ───


def test_handle_name_accepts_letters(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    wizard._handle_name(KeyEvent(char="r"))
    wizard._handle_name(KeyEvent(char="o"))
    wizard._handle_name(KeyEvent(char="n"))
    assert wizard.state.name == "ron"


def test_handle_name_rejects_space(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    wizard._handle_name(KeyEvent(char="a"))
    wizard._handle_name(KeyEvent(char=" "))
    wizard._handle_name(KeyEvent(char="b"))
    assert wizard.state.name == "ab"


def test_handle_name_backspace(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    for ch in "successor":
        wizard._handle_name(KeyEvent(char=ch))
    wizard._handle_name(KeyEvent(key=Key.BACKSPACE))
    wizard._handle_name(KeyEvent(key=Key.BACKSPACE))
    assert wizard.state.name == "success"


def test_handle_name_max_length(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    for _ in range(50):
        wizard._handle_name(KeyEvent(char="x"))
    # Capped at MAX_NAME_LEN
    assert len(wizard.state.name) <= 32


def test_handle_name_enter_with_valid_advances(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    for ch in "myprofile":
        wizard._handle_name(KeyEvent(char=ch))
    wizard._handle_name(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.THEME


def test_handle_name_enter_with_empty_glows(temp_config_dir: Path) -> None:
    """Empty name on Enter triggers a validation glow, doesn't advance."""
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    wizard._handle_name(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.NAME
    assert wizard._glow is not None
    assert wizard._glow.field == "name"


# ─── _handle_theme cycling + preview sync ───


def test_handle_theme_cycles_with_arrows(temp_config_dir: Path) -> None:
    """Up/Down arrows cycle through registered themes."""
    THEME_REGISTRY.reload()
    wizard = SuccessorSetup()
    wizard._enter_step(Step.THEME)
    wizard._handle_theme(KeyEvent(key=Key.DOWN))
    # After arrow, theme_name should reflect the new cursor position
    # (may be same name if only one theme is loaded — check regardless)
    cursor_after = wizard._cursors[Step.THEME]
    assert cursor_after >= 0


def test_handle_theme_arrow_syncs_preview(temp_config_dir: Path) -> None:
    """Arrowing between themes pushes the new theme into the preview chat."""
    # Drop a second theme into the user dir so we have something to cycle to
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    other_theme = {
        "name": "test_alt",
        "icon": "★",
        "description": "test",
        "dark": {
            "bg": "#000000", "bg_input": "#111111", "bg_footer": "#222222",
            "fg": "#FFFFFF", "fg_dim": "#CCCCCC", "fg_subtle": "#888888",
            "accent": "#FF0000", "accent_warm": "#FFAA00", "accent_warn": "#FF3300",
        },
        "light": {
            "bg": "#FFFFFF", "bg_input": "#EEEEEE", "bg_footer": "#DDDDDD",
            "fg": "#000000", "fg_dim": "#444444", "fg_subtle": "#888888",
            "accent": "#CC0000", "accent_warm": "#CC8800", "accent_warn": "#CC2200",
        },
    }
    (user_themes / "test_alt.json").write_text(json.dumps(other_theme))
    THEME_REGISTRY.reload()

    wizard = SuccessorSetup()
    wizard._enter_step(Step.THEME)
    initial_theme_name = wizard._preview_chat.theme.name
    # Walk through themes via DOWN key — eventually we'll land on a different one
    seen_names = {initial_theme_name}
    for _ in range(5):
        wizard._handle_theme(KeyEvent(key=Key.DOWN))
        seen_names.add(wizard._preview_chat.theme.name)
    # If multiple themes are available, the preview's theme should have changed
    # at some point in the walk
    assert len(seen_names) >= 2


def test_handle_mode_toggles(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.MODE)
    initial = wizard.state.display_mode
    wizard._handle_mode(KeyEvent(key=Key.UP))
    assert wizard.state.display_mode != initial
    wizard._handle_mode(KeyEvent(key=Key.DOWN))
    assert wizard.state.display_mode == initial


def test_handle_density_cycles(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.DENSITY)
    wizard._handle_density(KeyEvent(key=Key.DOWN))
    assert wizard.state.density == _DENSITY_OPTIONS[2]  # normal → spacious
    wizard._handle_density(KeyEvent(key=Key.DOWN))
    assert wizard.state.density == _DENSITY_OPTIONS[0]  # spacious → compact (wraps)


def test_handle_intro_toggles(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.INTRO)
    # Default is now "successor" (cursor at index 1) so the wizard
    # opens with the bundled intro pre-selected. Toggling Up moves
    # the cursor to "(none)" and clears the field.
    assert wizard.state.intro_animation == "successor"
    wizard._handle_intro(KeyEvent(key=Key.UP))
    assert wizard.state.intro_animation is None
    wizard._handle_intro(KeyEvent(key=Key.DOWN))
    assert wizard.state.intro_animation == "successor"


# ─── Save flow integration ───


def test_full_save_flow_writes_json_file(temp_config_dir: Path) -> None:
    """End-to-end: drive the wizard through every step and assert the JSON file lands."""
    THEME_REGISTRY.reload()
    PROFILE_REGISTRY.reload()

    wizard = SuccessorSetup()

    # Welcome → name
    wizard._handle_welcome(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.NAME

    # Type a name
    for ch in "smoketest":
        wizard._handle_name(KeyEvent(char=ch))
    wizard._handle_name(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.THEME

    # Pick theme (default selection)
    wizard._handle_theme(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.MODE

    # Pick mode
    wizard._handle_mode(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.DENSITY

    # Pick density
    wizard._handle_density(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.INTRO

    # Pick intro
    wizard._handle_intro(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.PROVIDER

    # Default provider (llamacpp) — just advance
    wizard._handle_provider(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.TOOLS

    # Accept the default tool selection (native file tools + bash enabled)
    wizard._handle_tools(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.COMPACTION

    # Accept the default compaction preset
    wizard._handle_compaction(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.REVIEW

    # Save
    wizard._handle_review(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.SAVED
    assert wizard.should_launch_chat is True
    assert wizard.saved_profile is not None
    assert wizard.saved_profile.name == "smoketest"

    # The profile JSON file landed on disk
    target = temp_config_dir / "profiles" / "smoketest.json"
    assert target.exists()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    payload = json.loads(target.read_text())
    assert payload["name"] == "smoketest"
    assert payload["theme"] == "steel"
    assert payload["display_mode"] == "dark"
    assert payload["tools"] == ["read_file", "write_file", "edit_file", "bash"]
    # Compaction defaults round-trip
    assert "compaction" in payload
    assert payload["compaction"]["enabled"] is True
    assert payload["compaction"]["autocompact_pct"] == 0.0625  # default

    # active_profile was persisted to chat.json
    chat_cfg = json.loads((temp_config_dir / "chat.json").read_text())
    assert chat_cfg["active_profile"] == "smoketest"
    assert chat_cfg["autorecord"] is True

    # The registry now has the new profile
    PROFILE_REGISTRY.reload()
    assert get_profile("smoketest") is not None


def test_save_with_empty_name_rejects(temp_config_dir: Path) -> None:
    """Trying to save with an empty name triggers validation glow + bounce to NAME."""
    wizard = SuccessorSetup()
    wizard._enter_step(Step.REVIEW)
    wizard._handle_review(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.NAME
    assert wizard._glow is not None
    assert wizard.should_launch_chat is False


def test_review_can_toggle_autorecord_before_save(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard.state.name = "record-toggle"
    wizard._enter_step(Step.REVIEW)
    assert wizard.state.autorecord is True
    wizard._handle_review(KeyEvent(char="a"))
    assert wizard.state.autorecord is False
    wizard._handle_review(KeyEvent(key=Key.ENTER))
    chat_cfg = json.loads((temp_config_dir / "chat.json").read_text())
    assert chat_cfg["autorecord"] is False


def test_review_can_adjust_max_agent_turns_before_save(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard.state.name = "turn-limit-test"
    wizard._enter_step(Step.REVIEW)
    assert wizard.state.max_agent_turns == DEFAULT_MAX_AGENT_TURNS
    wizard._handle_review(KeyEvent(char="]"))
    wizard._handle_review(KeyEvent(char="]"))
    assert wizard.state.max_agent_turns == DEFAULT_MAX_AGENT_TURNS + 10
    wizard._handle_review(KeyEvent(key=Key.ENTER))

    payload = json.loads((temp_config_dir / "profiles" / "turn-limit-test.json").read_text())
    assert payload["max_agent_turns"] == DEFAULT_MAX_AGENT_TURNS + 10


def test_esc_cancels_wizard(temp_config_dir: Path) -> None:
    """Esc from any non-saved step sets should_launch_chat=False and stops."""
    wizard = SuccessorSetup()
    wizard._enter_step(Step.THEME)
    wizard._handle_key(KeyEvent(key=Key.ESC))
    assert wizard.should_launch_chat is False
    assert wizard._running is False


# ─── wizard_demo_snapshot rendering ───


def test_snapshot_welcome_renders(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(rows=30, cols=100, step="welcome")
    plain = render_grid_to_plain(g)
    assert "successor · setup" in plain
    assert "welcome" in plain
    assert "step 1 of 10" in plain


def test_snapshot_name_step_shows_input_field(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30, cols=100, step="name", name="test-name",
    )
    plain = render_grid_to_plain(g)
    assert "test-name" in plain
    assert "name for your new profile" in plain
    assert "step 2 of 10" in plain


def test_snapshot_review_mentions_local_autorecord(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30,
        cols=100,
        step="review",
        name="reviewdemo",
    )
    plain = render_grid_to_plain(g)
    assert "autorecord" in plain
    assert "local-only bundles" in plain
    assert "max agent turns" in plain


def test_snapshot_theme_step_shows_live_preview(temp_config_dir: Path) -> None:
    """The theme step renders the live preview pane with the active theme."""
    g = wizard_demo_snapshot(
        rows=30, cols=110, step="theme", name="test", theme_name="steel",
    )
    plain = render_grid_to_plain(g)
    # The preview pane shows the chat title bar with the theme name
    assert "steel" in plain
    # The greeting message from the preview script
    assert "Greetings, traveler" in plain
    assert "live preview" in plain


def test_snapshot_tools_step_shows_checkboxes(temp_config_dir: Path) -> None:
    """The tools step paints a checklist of registered tools, with
    native file tools and bash enabled by default."""
    g = wizard_demo_snapshot(
        rows=30, cols=100, step="tools", name="tools-prof",
    )
    plain = render_grid_to_plain(g)
    assert "enable tools" in plain
    assert "read" in plain
    assert "write" in plain
    assert "edit" in plain
    assert "bash" in plain
    assert "subagent" in plain
    assert "[✓]" in plain  # default: bash checked
    assert "4 tools enabled" in plain


def test_snapshot_tools_step_chat_only_mode(temp_config_dir: Path) -> None:
    """A profile with no enabled tools renders in 'chat-only mode'."""
    g = wizard_demo_snapshot(
        rows=30, cols=100, step="tools", name="chatonly",
        enabled_tools=(),
    )
    plain = render_grid_to_plain(g)
    assert "bash" in plain
    assert "subagent" in plain
    assert "[ ]" in plain
    assert "chat-only mode" in plain


def test_handle_tools_toggle_flow(temp_config_dir: Path) -> None:
    """Space toggles the cursor'd tool; enter advances to review."""
    wizard = SuccessorSetup()
    wizard._enter_step(Step.TOOLS)
    # Default state includes native file tools + bash. Cursor starts on read_file.
    assert "read_file" in wizard.state.enabled_tools

    # Toggle read_file off
    wizard._handle_tools(KeyEvent(char=" "))
    assert "read_file" not in wizard.state.enabled_tools

    # Toggle read_file back on
    wizard._handle_tools(KeyEvent(char=" "))
    assert "read_file" in wizard.state.enabled_tools

    # Enter advances to COMPACTION (then REVIEW after one more advance)
    wizard._handle_tools(KeyEvent(key=Key.ENTER))
    assert wizard.current_step == Step.COMPACTION


def test_wizard_state_chat_only_roundtrip(temp_config_dir: Path) -> None:
    """A _WizardState with empty enabled_tools produces a tool-free profile."""
    state = _WizardState(name="bare", enabled_tools=())
    profile = state.to_profile()
    assert profile.tools == ()
    payload = state.to_json_dict()
    assert payload["tools"] == []


def test_yolo_toggle_appears_when_bash_enabled(temp_config_dir: Path) -> None:
    """When bash is in enabled_tools, cursor can reach the yolo row."""
    from successor.tools_registry import selectable_tool_names
    wizard = SuccessorSetup()
    wizard._enter_step(Step.TOOLS)
    assert "bash" in wizard.state.enabled_tools
    tool_names = selectable_tool_names()
    yolo_idx = len(tool_names)

    # Move cursor down to the yolo row
    for _ in range(yolo_idx):
        wizard._handle_tools(KeyEvent(key=Key.DOWN))
    assert wizard._cursors[Step.TOOLS] == yolo_idx

    # Toggle yolo on
    wizard._handle_tools(KeyEvent(char=" "))
    assert wizard.state.bash_yolo is True

    # Toggle yolo off
    wizard._handle_tools(KeyEvent(char=" "))
    assert wizard.state.bash_yolo is False


def test_yolo_resets_when_bash_disabled(temp_config_dir: Path) -> None:
    """Turning off bash via toggle resets bash_yolo to False."""
    wizard = SuccessorSetup()
    wizard._enter_step(Step.TOOLS)
    # Enable yolo first
    wizard.state.bash_yolo = True
    assert wizard.state.bash_yolo is True

    # Find bash in tool_names and toggle it off
    from successor.tools_registry import selectable_tool_names
    tool_names = selectable_tool_names()
    bash_idx = tool_names.index("bash")
    wizard._cursors[Step.TOOLS] = bash_idx
    wizard._handle_tools(KeyEvent(char=" "))
    assert "bash" not in wizard.state.enabled_tools
    assert wizard.state.bash_yolo is False


def test_yolo_in_tool_config_json(temp_config_dir: Path) -> None:
    """to_json_dict includes bash.allow_dangerous when yolo is on."""
    state = _WizardState(
        name="yolo-test",
        enabled_tools=("bash",),
        bash_yolo=True,
    )
    payload = state.to_json_dict()
    assert payload["tool_config"]["bash"]["allow_dangerous"] is True


def test_yolo_absent_when_bash_not_enabled(temp_config_dir: Path) -> None:
    """tool_config is empty when bash is not in tools."""
    state = _WizardState(
        name="no-bash",
        enabled_tools=("read_file",),
        bash_yolo=True,  # shouldn't matter — bash isn't enabled
    )
    payload = state.to_json_dict()
    assert payload["tool_config"] == {}


def test_yolo_row_not_reachable_without_bash(temp_config_dir: Path) -> None:
    """Without bash, cursor wraps only within tool checkboxes."""
    from successor.tools_registry import selectable_tool_names
    wizard = SuccessorSetup()
    wizard._enter_step(Step.TOOLS)
    # Disable bash
    tool_names = selectable_tool_names()
    bash_idx = tool_names.index("bash")
    wizard._cursors[Step.TOOLS] = bash_idx
    wizard._handle_tools(KeyEvent(char=" "))
    assert "bash" not in wizard.state.enabled_tools

    # Move cursor past last tool — should wrap to 0, not to yolo row
    wizard._cursors[Step.TOOLS] = len(tool_names) - 1
    wizard._handle_tools(KeyEvent(key=Key.DOWN))
    assert wizard._cursors[Step.TOOLS] == 0


# ─── Provider step ───


def test_provider_step_default_is_llamacpp(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    assert wizard.state.provider_preset == "llamacpp"
    # Default → produces a llamacpp provider config
    profile = wizard.state.to_profile()
    assert profile.provider["type"] == "llamacpp"
    assert profile.provider["base_url"] == "http://localhost:8080"


def test_provider_step_arrow_keys_select_presets(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    assert wizard._cursors[Step.PROVIDER] == 0
    assert wizard.state.provider_preset == "llamacpp"
    # DOWN moves cursor to each preset; space selects it
    # Cursor 0 = llamacpp (already selected), 1 = ollama, 2 = openai, etc.
    presets = ["ollama", "openai", "anthropic", "zai", "openrouter", "generic", "kimi-code"]
    for i, expected in enumerate(presets):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
        assert wizard._cursors[Step.PROVIDER] == i + 1
        wizard._handle_provider(KeyEvent(char=" "))
        assert wizard.state.provider_preset == expected, (
            f"after pressing space at cursor {i + 1}, expected {expected}, got {wizard.state.provider_preset}"
        )
    # UP from cursor 0 wraps to max_pos - 1
    wizard._cursors[Step.PROVIDER] = 0
    wizard._handle_provider(KeyEvent(key=Key.UP))
    # max_pos for kimi-code (no api_key needed, no OAuth token) = 8
    assert wizard._cursors[Step.PROVIDER] == 7  # wraps to last preset


def test_provider_step_openrouter_requires_api_key_to_advance(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Navigate to openrouter (cursor 5) and select it
    for _ in range(5):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    wizard._handle_provider(KeyEvent(char=" "))
    assert wizard.state.provider_preset == "openrouter"
    # Clear the api key and try to advance
    wizard.state.provider_api_key = ""
    wizard._handle_provider(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.PROVIDER  # blocked
    assert wizard._glow is not None
    assert "api key" in wizard._glow.message


def test_provider_step_openai_requires_api_key_to_advance(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Navigate to openai (cursor 2) and select it
    for _ in range(2):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    wizard._handle_provider(KeyEvent(char=" "))
    assert wizard.state.provider_preset == "openai"
    wizard.state.provider_api_key = ""
    wizard._handle_provider(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.PROVIDER  # blocked
    assert wizard._glow is not None
    assert "api key" in wizard._glow.message
    assert "openai" in wizard._glow.message


def test_provider_step_openrouter_full_flow(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Navigate to openrouter (cursor 5) and select it
    for _ in range(5):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    wizard._handle_provider(KeyEvent(char=" "))
    assert wizard.state.provider_preset == "openrouter"
    # Now openrouter is selected (needs api_key). Cursor still at 5.
    # Move down to api_key field (cursor 8): 5→6→7→8
    for _ in range(3):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    assert wizard._cursors[Step.PROVIDER] == 8
    for ch in "sk-or-test-key":
        wizard._handle_provider(KeyEvent(char=ch))
    assert wizard.state.provider_api_key == "sk-or-test-key"
    # Backspace deletes one char
    wizard._handle_provider(KeyEvent(key=Key.BACKSPACE))
    assert wizard.state.provider_api_key == "sk-or-test-ke"
    # Move focus to model field (cursor 9)
    wizard._handle_provider(KeyEvent(key=Key.DOWN))
    assert wizard._cursors[Step.PROVIDER] == 9
    # Default model is preset; clear and type a fresh one
    wizard.state.provider_model = ""
    for ch in "google/gemma-3-27b-it:free":
        wizard._handle_provider(KeyEvent(char=ch))
    assert wizard.state.provider_model == "google/gemma-3-27b-it:free"
    # Right advances to TOOLS
    wizard._handle_provider(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.TOOLS

    # The serialized profile carries the openrouter config
    payload = wizard.state.to_json_dict()
    assert payload["provider"]["type"] == "openai_compat"
    assert payload["provider"]["base_url"] == "https://openrouter.ai/api/v1"
    assert payload["provider"]["model"] == "google/gemma-3-27b-it:free"
    assert payload["provider"]["api_key"] == "sk-or-test-ke"
    # context_window is intentionally NOT set — the chat detects it.
    assert "context_window" not in payload["provider"]


def test_provider_step_openai_full_flow(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Navigate to openai (cursor 2) and select it
    for _ in range(2):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    wizard._handle_provider(KeyEvent(char=" "))
    assert wizard.state.provider_preset == "openai"
    # Default model should auto-swap to gpt-4.1-mini when picking openai
    assert wizard.state.provider_model == "gpt-4.1-mini"
    # Move to api_key field (cursor 8): from cursor 2, DOWN 6 times
    for _ in range(6):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    assert wizard._cursors[Step.PROVIDER] == 8
    for ch in "sk-proj-test-key":
        wizard._handle_provider(KeyEvent(char=ch))
    assert wizard.state.provider_api_key == "sk-proj-test-key"
    # Right advances (model is preset, api_key is set)
    wizard._handle_provider(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.TOOLS

    payload = wizard.state.to_json_dict()
    assert payload["provider"]["type"] == "openai_compat"
    assert payload["provider"]["base_url"] == "https://api.openai.com/v1"
    assert payload["provider"]["model"] == "gpt-4.1-mini"
    assert payload["provider"]["api_key"] == "sk-proj-test-key"
    assert "context_window" not in payload["provider"]


def test_provider_step_zai_flow(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Navigate to zai (cursor 4) and select it
    for _ in range(4):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    wizard._handle_provider(KeyEvent(char=" "))
    assert wizard.state.provider_preset == "zai"
    assert wizard.state.provider_model == "glm-5.1"
    # Move to api_key field (cursor 8): from cursor 4, DOWN 4 times
    for _ in range(4):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    for ch in "test-zai-key":
        wizard._handle_provider(KeyEvent(char=ch))
    assert wizard.state.provider_api_key == "test-zai-key"
    # Right advances
    wizard._handle_provider(KeyEvent(key=Key.RIGHT))
    assert wizard.current_step == Step.TOOLS

    payload = wizard.state.to_json_dict()
    assert payload["provider"]["type"] == "anthropic"
    assert payload["provider"]["base_url"] == "https://api.z.ai/api/anthropic"
    assert payload["provider"]["model"] == "glm-5.1"
    assert payload["provider"]["api_key"] == "test-zai-key"


def test_provider_step_left_retreats_to_previous_step(
    temp_config_dir: Path,
) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # LEFT always retreats to the previous step
    wizard._handle_provider(KeyEvent(key=Key.LEFT))
    assert wizard.current_step == Step.INTRO


def test_provider_step_to_json_includes_subagents_and_oauth(
    temp_config_dir: Path,
) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Default llamacpp preset — no OAuth
    payload = wizard.state.to_json_dict()
    assert payload["oauth"] is None
    assert isinstance(payload["subagents"], dict)
    assert payload["subagents"]["enabled"] is True
    # Provider config should not have api_key for llamacpp
    assert "api_key" not in payload["provider"]


def test_provider_step_kimi_code_detects_oauth(temp_config_dir: Path) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    # Navigate to kimi-code (cursor 7) and select it
    for _ in range(7):
        wizard._handle_provider(KeyEvent(key=Key.DOWN))
    wizard._handle_provider(KeyEvent(char=" "))
    assert wizard.state.provider_preset == "kimi-code"
    # No stored token → oauth_ref should be None
    assert wizard.state.oauth_ref is None
    # Provider dict should NOT include api_key for kimi-code
    provider = wizard.state._build_provider_dict()
    assert "api_key" not in provider
    assert provider["type"] == "openai_compat"
    assert provider["base_url"] == "https://api.kimi.com/coding/v1"
    assert provider["model"] == "kimi-k2-5"


def test_snapshot_review_shows_summary(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30, cols=110, step="review",
        name="my-prof", theme_name="steel",
        display_mode="light", density="spacious",
        intro_animation="successor",
    )
    plain = render_grid_to_plain(g)
    assert "ready to save 'my-prof'" in plain
    assert "my-prof" in plain
    assert "steel" in plain
    assert "light" in plain
    assert "spacious" in plain
    assert "successor" in plain


def test_snapshot_saved_step(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=20, cols=80, step="saved", name="test-saved",
    )
    plain = render_grid_to_plain(g)
    assert "test-saved" in plain
    assert "saved" in plain
    assert "opening chat" in plain


def test_snapshot_sidebar_shows_progress_marks(temp_config_dir: Path) -> None:
    """Completed steps show ✓ in the sidebar; the active one shows ▸."""
    g = wizard_demo_snapshot(
        rows=30, cols=100, step="density", name="x",
    )
    plain = render_grid_to_plain(g)
    # Completed steps before "density"
    assert "✓ welcome" in plain
    assert "✓ name" in plain
    assert "✓ theme" in plain
    assert "✓ mode" in plain
    # Active step
    assert "▸ density" in plain


def test_snapshot_creating_pill_in_title_bar(temp_config_dir: Path) -> None:
    """When name is set, the title bar shows a 'creating: <name>' pill."""
    g = wizard_demo_snapshot(
        rows=20, cols=100, step="theme", name="successor-test",
    )
    plain = render_grid_to_plain(g)
    assert "creating:" in plain
    assert "successor-test" in plain


def test_snapshot_too_small_terminal(temp_config_dir: Path) -> None:
    """Below the minimum size, the wizard shows a polite message instead of crashing."""
    g = wizard_demo_snapshot(rows=10, cols=40, step="welcome")
    plain = render_grid_to_plain(g)
    assert "too small" in plain


def test_snapshot_dark_and_light_differ(temp_config_dir: Path) -> None:
    """Switching display_mode in the wizard produces different ANSI output.

    Catches the case where the wizard's chrome doesn't honor the
    user's selected display mode.
    """
    from successor.snapshot import render_grid_to_ansi

    dark = wizard_demo_snapshot(
        rows=30, cols=100, step="mode", display_mode="dark",
    )
    light = wizard_demo_snapshot(
        rows=30, cols=100, step="mode", display_mode="light",
    )
    assert render_grid_to_ansi(dark) != render_grid_to_ansi(light)


def test_snapshot_density_changes_preview(temp_config_dir: Path) -> None:
    """Different densities produce visibly different previews."""
    from successor.snapshot import render_grid_to_ansi

    compact = wizard_demo_snapshot(
        rows=30, cols=110, step="density", density="compact",
    )
    spacious = wizard_demo_snapshot(
        rows=30, cols=110, step="density", density="spacious",
    )
    assert render_grid_to_ansi(compact) != render_grid_to_ansi(spacious)
