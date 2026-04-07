"""Tests for friendly StreamError rendering.

The most common first-run failure is "user opens chat, types a message,
gets [stream failed: connection failed: <urlopen error [Errno 111]
Connection refused>]". The chat translates that into an actionable
hint that names the expected base_url and the llama.cpp quickstart.
"""

from __future__ import annotations

from pathlib import Path

from successor.chat import SuccessorChat
from successor.providers.llama import StreamError


def test_friendly_error_for_connection_refused(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    msg = chat._format_stream_error(
        "connection failed: <urlopen error [Errno 111] Connection refused>"
    )
    assert "no llama.cpp server" in msg
    assert "http://localhost:8080" in msg
    assert "llama-server" in msg
    assert "/config" in msg


def test_friendly_error_for_dns_failure(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    msg = chat._format_stream_error(
        "connection failed: <urlopen error [Errno -2] Name or service not known>"
    )
    assert "no llama.cpp server" in msg
    assert "llama-server" in msg


def test_friendly_error_uses_active_profile_base_url(temp_config_dir: Path) -> None:
    """If the profile points at a non-default endpoint, the hint
    quotes that URL so the user can verify what the chat is trying."""
    chat = SuccessorChat()
    chat.profile.provider["base_url"] = "http://10.0.0.5:9090"
    msg = chat._format_stream_error("connection failed: connection refused")
    assert "http://10.0.0.5:9090" in msg
    assert "http://localhost:8080" not in msg


def test_other_errors_pass_through(temp_config_dir: Path) -> None:
    """A non-connection error (e.g. HTTP 400) is reported as-is —
    we only special-case the noisy/unhelpful socket failures."""
    chat = SuccessorChat()
    msg = chat._format_stream_error("HTTP 400: Bad Request")
    assert msg == "[stream failed: HTTP 400: Bad Request]"


def test_stream_error_event_renders_friendly_message(temp_config_dir: Path) -> None:
    """End-to-end through _pump_stream: a StreamError event for a
    connection failure produces an assistant message with the friendly
    hint, not the raw urllib error."""
    from successor.chat import _Message  # noqa: F401
    chat = SuccessorChat()
    chat.messages = []

    class _ErrorStream:
        def drain(self) -> list:
            return [StreamError(message="connection failed: connection refused")]

        def close(self) -> None:
            pass

    chat._stream = _ErrorStream()  # type: ignore[assignment]
    chat._stream_content = []
    chat._pump_stream()

    assert chat._stream is None
    error_msgs = [m for m in chat.messages if m.role == "successor"]
    assert error_msgs, "expected an assistant error message"
    body = error_msgs[-1].raw_text
    assert "no llama.cpp server" in body
    assert "llama-server" in body
    # The raw urllib error should NOT bleed through.
    assert "Errno" not in body
    assert "urlopen" not in body
