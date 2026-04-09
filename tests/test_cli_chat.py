"""CLI chat launch behavior tests."""

from __future__ import annotations

import argparse

from successor.cli import cmd_chat
from successor.profiles import Profile


def test_cmd_chat_forwards_intro_slash_prefill(monkeypatch) -> None:
    profile = Profile(name="cli-test", intro_animation="successor")
    created: list[object] = []

    class _FakeChat:
        def __init__(self, *, profile, initial_input="") -> None:
            self.profile = profile
            self.initial_input = initial_input
            self._pending_action = None
            created.append(self)

        def run(self) -> None:
            return

    monkeypatch.setattr("successor.profiles.get_active_profile", lambda: profile)
    monkeypatch.setattr("successor.chat.SuccessorChat", _FakeChat)
    monkeypatch.setattr("successor.cli._play_intro_animation", lambda name: "/")

    assert cmd_chat(argparse.Namespace()) == 0
    assert len(created) == 1
    assert created[0].initial_input == "/"


def test_cmd_chat_does_not_prefill_without_intro(monkeypatch) -> None:
    profile = Profile(name="cli-test", intro_animation=None)
    created: list[object] = []

    class _FakeChat:
        def __init__(self, *, profile, initial_input="") -> None:
            self.profile = profile
            self.initial_input = initial_input
            self._pending_action = None
            created.append(self)

        def run(self) -> None:
            return

    monkeypatch.setattr("successor.profiles.get_active_profile", lambda: profile)
    monkeypatch.setattr("successor.chat.SuccessorChat", _FakeChat)

    assert cmd_chat(argparse.Namespace()) == 0
    assert len(created) == 1
    assert created[0].initial_input == ""
