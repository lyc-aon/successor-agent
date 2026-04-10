"""Terminal session escape-sequence coverage."""

from __future__ import annotations

from successor.render import terminal as term_mod


def test_terminal_enables_alternate_scroll_when_mouse_is_off(monkeypatch) -> None:
    writes: list[str] = []

    monkeypatch.setattr(term_mod.atexit, "register", lambda fn: None)
    monkeypatch.setattr(term_mod.signal, "signal", lambda *args, **kwargs: None)

    term = term_mod.Terminal(
        raw=False,
        alt_screen=True,
        bracketed_paste=False,
        mouse_reporting=False,
    )
    monkeypatch.setattr(term, "write", writes.append)

    term.__enter__()
    term.__exit__(None, None, None)

    assert writes
    assert writes[0].startswith(
        term_mod.ALT_SCROLL_SAVE + term_mod.ALT_SCROLL_ON + term_mod.ALT_SCREEN_ON
    )
    assert writes[-1].endswith(term_mod.ALT_SCREEN_OFF + term_mod.ALT_SCROLL_RESTORE)


def test_terminal_emits_mouse_reporting_sequences_when_enabled(monkeypatch) -> None:
    writes: list[str] = []

    monkeypatch.setattr(term_mod.atexit, "register", lambda fn: None)
    monkeypatch.setattr(term_mod.signal, "signal", lambda *args, **kwargs: None)

    term = term_mod.Terminal(
        raw=False,
        alt_screen=True,
        bracketed_paste=False,
        mouse_reporting=True,
    )
    monkeypatch.setattr(term, "write", writes.append)

    term.__enter__()
    term.__exit__(None, None, None)

    assert writes
    assert term_mod.ALT_SCROLL_OFF in writes[0]
    assert term_mod.MOUSE_ON in writes[0]
    assert term_mod.MOUSE_OFF in writes[-1]


def test_runtime_mouse_toggle_switches_alt_scroll_too(monkeypatch) -> None:
    writes: list[str] = []

    term = term_mod.Terminal(
        raw=False,
        alt_screen=True,
        bracketed_paste=False,
        mouse_reporting=False,
    )
    monkeypatch.setattr(term, "write", writes.append)

    term.set_mouse_reporting(True)
    term.set_mouse_reporting(False)

    assert writes == [
        term_mod.ALT_SCROLL_OFF + term_mod.MOUSE_ON,
        term_mod.MOUSE_OFF + term_mod.ALT_SCROLL_ON,
    ]
