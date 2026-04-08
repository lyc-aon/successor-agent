"""Terminal session escape-sequence coverage."""

from __future__ import annotations

from successor.render import terminal as term_mod


def test_terminal_disables_alternate_scroll_for_session(monkeypatch) -> None:
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
        term_mod.ALT_SCROLL_SAVE + term_mod.ALT_SCROLL_OFF + term_mod.ALT_SCREEN_ON
    )
    assert writes[-1].endswith(term_mod.ALT_SCREEN_OFF + term_mod.ALT_SCROLL_RESTORE)
