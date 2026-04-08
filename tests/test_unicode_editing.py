"""Unicode editing regressions across user-facing input surfaces."""

from __future__ import annotations

from pathlib import Path

from successor.chat import SuccessorChat
from successor.input.keys import Key, KeyEvent
from successor.profiles import Profile
from successor.wizard.config import _SETTINGS_TREE, Focus, SuccessorConfig
from successor.wizard.prompt_editor import PromptEditor
from successor.wizard.setup import Step, SuccessorSetup


def _new_chat(temp_config_dir: Path) -> SuccessorChat:
    return SuccessorChat(profile=Profile(name="unicode-test"))


def _field_idx(name: str) -> int:
    return next(i for i, field in enumerate(_SETTINGS_TREE) if field.name == name)


def test_chat_backspace_deletes_full_decomposed_grapheme(
    temp_config_dir: Path,
) -> None:
    chat = _new_chat(temp_config_dir)
    chat.input_buffer = "cafe\u0301"
    chat._handle_key_event(KeyEvent(key=Key.BACKSPACE))
    assert chat.input_buffer == "caf"


def test_search_backspace_deletes_full_decomposed_grapheme(
    temp_config_dir: Path,
) -> None:
    chat = _new_chat(temp_config_dir)
    chat._search_open()
    chat._handle_key_event(KeyEvent(char="cafe\u0301"))
    chat._handle_key_event(KeyEvent(key=Key.BACKSPACE))
    assert chat._search_query == "caf"


def test_prompt_editor_backspace_deletes_full_decomposed_grapheme() -> None:
    ed = PromptEditor("cafe\u0301")
    ed.cursor_col = len(ed.lines[0])
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines == ["caf"]
    assert ed.cursor_col == 3


def test_prompt_editor_delete_deletes_full_decomposed_grapheme() -> None:
    ed = PromptEditor("cafe\u0301!")
    ed.cursor_col = 3
    ed.handle_key(KeyEvent(key=Key.DELETE))
    assert ed.lines == ["caf!"]
    assert ed.cursor_col == 3


def test_config_inline_backspace_deletes_full_decomposed_grapheme(
    temp_config_dir: Path,
) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    menu._inline_text_edit.buffer = "cafe\u0301"
    menu._inline_text_edit.cursor = len(menu._inline_text_edit.buffer)
    menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    assert menu._inline_text_edit.buffer == "caf"
    assert menu._inline_text_edit.cursor == 3


def test_wizard_name_backspace_deletes_full_decomposed_grapheme(
    temp_config_dir: Path,
) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.NAME)
    wizard.state.name = "cafe\u0301"
    wizard._handle_name(KeyEvent(key=Key.BACKSPACE))
    assert wizard.state.name == "caf"


def test_wizard_provider_backspace_deletes_full_decomposed_grapheme(
    temp_config_dir: Path,
) -> None:
    wizard = SuccessorSetup()
    wizard._enter_step(Step.PROVIDER)
    wizard._handle_provider(KeyEvent(char=" "))  # toggle to openai
    wizard._handle_provider(KeyEvent(key=Key.DOWN))  # api_key field
    wizard.state.provider_api_key = "cafe\u0301"
    wizard._handle_provider(KeyEvent(key=Key.BACKSPACE))
    assert wizard.state.provider_api_key == "caf"
