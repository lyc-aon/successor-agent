"""Tests for the chat ↔ bash integration.

Two layers:
  1. /bash slash command in _submit dispatches dispatch_bash and
     appends a tool message
  2. _build_message_lines + _paint_chat_row pre-paint tool cards
     into _RenderedRows with prepainted_cells

Hermetic via temp_config_dir; uses real subprocess.run() against
shell builtins (echo, true, pwd) so no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from successor.agent.bash_stream import BashStreamDetector
from successor.bash import ToolCard, dispatch_bash, preview_bash
from successor.chat import SuccessorChat, _Message
from successor.providers.llama import ContentChunk, StreamEnded, StreamStarted
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain


class _FakeStream:
    """Minimal ChatStream stand-in for driving _pump_stream in tests.

    Only implements the surface _pump_stream touches: a `drain()`
    method that returns events. Tests build a FakeStream with a
    preloaded event list and hand it to chat._stream.
    """

    def __init__(self, events: list) -> None:
        self._events = list(events)

    def drain(self) -> list:
        out = list(self._events)
        self._events = []
        return out

    def close(self) -> None:
        pass


def _content(text: str) -> ContentChunk:
    return ContentChunk(text=text)


def _stream_end() -> StreamEnded:
    return StreamEnded(finish_reason="stop")


# ─── /bash slash command ───


def test_bash_command_appends_tool_message(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash echo hello"
    chat._submit()

    # Should have appended: synthetic user echo + tool message
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    card = tool_msgs[0].tool_card
    assert card.verb == "print-text"
    assert card.exit_code == 0
    assert "hello" in card.output


def test_bash_command_no_args_shows_usage(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash"
    chat._submit()
    # No tool card — just a usage hint
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 0
    assert any("usage:" in m.raw_text for m in chat.messages)


def test_bash_command_blank_arg_shows_usage(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash   "
    chat._submit()
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 0


def test_bash_dangerous_command_appends_refused_card(temp_config_dir: Path) -> None:
    """A dangerous command appends the REFUSED card so the user can
    see what was blocked, plus a synthetic explanation message."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash sudo rm -rf /"
    chat._submit()

    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    card = tool_msgs[0].tool_card
    assert card.risk == "dangerous"
    # NOT executed because it was refused
    assert not card.executed
    # Refusal message follows
    assert any("refused" in m.raw_text for m in chat.messages)


def test_bash_command_does_not_send_to_model(temp_config_dir: Path) -> None:
    """Tool messages must be marked synthetic so they're never sent
    to the model in the conversation history."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash echo only_for_testing"
    chat._submit()
    for msg in chat.messages:
        if msg.tool_card is not None:
            assert msg.synthetic, "tool messages must be synthetic"


def test_tool_card_message_construction_forces_synthetic() -> None:
    """Constructing a _Message with tool_card auto-sets synthetic=True."""
    card = preview_bash("ls")
    msg = _Message("tool", "", tool_card=card)
    assert msg.synthetic
    assert msg.tool_card is card


# ─── Pre-painted row pipeline ───


def test_tool_card_renders_in_chat_grid(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message("user", "show pwd", synthetic=True))
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("pwd"),
    ))

    g = Grid(30, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    # Card structure visible
    assert "working-directory" in plain
    assert "$ pwd" in plain
    assert "exit 0" in plain


def test_tool_card_row_prepainted_cells_present(temp_config_dir: Path) -> None:
    """The row builder must produce rows with prepainted_cells set
    for tool messages."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("echo testing"),
    ))
    rows = chat._build_message_lines(80, chat._current_variant())
    tool_rows = [r for r in rows if r.line_tag == "tool_card"]
    assert len(tool_rows) > 0
    for r in tool_rows:
        assert len(r.prepainted_cells) > 0


def test_multiple_tool_cards_stack(temp_config_dir: Path) -> None:
    """Multiple tool messages render stacked vertically without overlap."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message("tool", "", tool_card=dispatch_bash("echo first")))
    chat.messages.append(_Message("tool", "", tool_card=dispatch_bash("echo second")))

    g = Grid(40, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    assert "first" in plain
    assert "second" in plain
    # Both cards' raw commands visible on their bottom borders
    assert "$ echo first" in plain
    assert "$ echo second" in plain


def test_tool_card_in_session_with_regular_messages(temp_config_dir: Path) -> None:
    """Mix tool cards with regular markdown messages — both render."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message("user", "what's the cwd?", synthetic=True))
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("pwd"),
    ))
    chat.messages.append(_Message(
        "successor", "that's the project root.", synthetic=True,
    ))

    g = Grid(40, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    assert "what's the cwd?" in plain
    assert "$ pwd" in plain
    assert "project root" in plain


def test_failed_tool_card_renders_failure_glyph(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("false"),
    ))
    g = Grid(20, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    assert "exit 1" in plain
    assert "✗" in plain


def test_unknown_command_renders_with_question_badge(temp_config_dir: Path) -> None:
    """Commands without a registered parser render as the generic
    'bash ?' card and still execute."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("wc -l README.md"),
    ))
    g = Grid(20, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    # The generic bash card has the verb "bash"
    assert "bash" in plain
    assert "$ wc -l README.md" in plain
    # And the question badge
    assert "?" in plain


# ─── Streamed bash detection ───


def test_stream_end_dispatches_detected_bash_block(tmp_path) -> None:
    """When bash is in profile.tools, streamed ```bash blocks are
    detected and become tool cards after StreamEnded."""
    chat = SuccessorChat()
    chat.messages = []
    # Simulate the bash detector being primed by _submit
    chat._stream_bash_detector = BashStreamDetector()
    # Pretend a stream is in flight with chunks that contain a fenced
    # bash block. The detector feeds off ContentChunk events.
    chat._stream = _FakeStream([
        _content("sure, let me check\n\n"),
        _content("```bash\n"),
        _content("echo hello\n"),
        _content("```\n"),
        _stream_end(),
    ])
    chat._pump_stream()
    # The assistant message should be committed first
    assert any(m.role == "successor" for m in chat.messages)
    # And a tool card for the echo command should have been appended
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    assert "echo" in tool_msgs[0].tool_card.raw_command


def test_stream_end_dispatches_multiple_blocks(tmp_path) -> None:
    """Multiple bash blocks in one stream become separate tool cards
    in emission order."""
    chat = SuccessorChat()
    chat.messages = []
    chat._stream_bash_detector = BashStreamDetector()
    chat._stream = _FakeStream([
        _content("step one:\n```bash\necho one\n```\n"),
        _content("and step two:\n```bash\necho two\n```\n"),
        _stream_end(),
    ])
    chat._pump_stream()
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 2
    assert "echo one" in tool_msgs[0].tool_card.raw_command
    assert "echo two" in tool_msgs[1].tool_card.raw_command


def test_stream_without_detector_ignores_bash_fences() -> None:
    """If bash is NOT enabled (detector is None), fenced bash blocks
    in the stream are treated as plain text — no tool cards."""
    chat = SuccessorChat()
    chat.messages = []
    chat._stream_bash_detector = None  # chat-only mode
    chat._stream = _FakeStream([
        _content("here's some bash:\n```bash\nrm -rf /\n```\n"),
        _stream_end(),
    ])
    chat._pump_stream()
    # No tool cards should be created
    assert all(m.tool_card is None for m in chat.messages)
    # The assistant message should still land
    assert any(m.role == "successor" for m in chat.messages)


def test_dangerous_command_in_stream_shows_refusal() -> None:
    """A dangerous command streamed by the model is caught at dispatch
    time — the refused card is shown plus a synthetic note."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    # Pin a hermetic profile — otherwise we inherit whatever the
    # user's real ~/.config/successor/profiles/default.json has,
    # which may have allow_dangerous flipped on from their last
    # actual chat session.
    chat.profile = Profile(name="hermetic-test")
    chat.messages = []
    chat._stream_bash_detector = BashStreamDetector()
    chat._stream = _FakeStream([
        _content("let me clean up\n"),
        _content("```bash\nrm -rf /\n```\n"),
        _stream_end(),
    ])
    chat._pump_stream()
    # The refused card should appear
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_card.risk == "dangerous"
    # And a synthetic 'refused' note
    synthetic = [m for m in chat.messages if m.synthetic and "refused" in m.raw_text.lower()]
    assert len(synthetic) >= 1


def test_yolo_profile_lets_dangerous_command_through_stream() -> None:
    """When profile.tool_config[bash].allow_dangerous is True, the
    classifier's refusal is SKIPPED and the command runs for real.

    We use `eval ""` as a hermetic proxy for a dangerous command —
    it trips the classifier but doesn't actually do anything. The
    test verifies the refusal path is bypassed in yolo mode.
    """
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolobro",
        tool_config={"bash": {"allow_dangerous": True}},
    )
    chat.messages = []
    chat._stream_bash_detector = BashStreamDetector()
    chat._stream = _FakeStream([
        _content('```bash\neval ""\n```\n'),
        _stream_end(),
    ])
    chat._pump_stream()
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    # Critically: the card EXECUTED (has an exit_code) instead of
    # being a refusal preview
    assert tool_msgs[0].tool_card.executed
    # No 'refused' synthetic message this time
    synthetic = [m for m in chat.messages if m.synthetic and "refused" in m.raw_text.lower()]
    assert synthetic == []


def test_heredoc_file_write_produces_one_card_and_writes_file(tmp_path) -> None:
    """REGRESSION (phase 5.8.1): a heredoc HTML file write used to
    explode into one tool card per heredoc line, each failing with
    'command not found' on the HTML tag lines. After the fix, the
    whole heredoc is dispatched as one bash command, creating exactly
    one tool card AND writing the real file to disk.
    """
    from successor.profiles import Profile
    target = tmp_path / "successor.html"

    chat = SuccessorChat()
    # Yolo mode so the `>` redirect (mutating) isn't refused
    chat.profile = Profile(
        name="yolo",
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []
    chat._stream_bash_detector = BashStreamDetector()

    # This is what the model typically emits when asked to write
    # an HTML file
    model_reply = (
        "I'll create that HTML file for you.\n\n"
        "```bash\n"
        f"cat > {target} <<'EOF'\n"
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head><title>Successor</title></head>\n"
        "<body><h1>Hello from Successor</h1></body>\n"
        "</html>\n"
        "EOF\n"
        "```\n\n"
        "Done!\n"
    )
    chat._stream = _FakeStream([
        _content(model_reply),
        _stream_end(),
    ])
    chat._pump_stream()

    # EXACTLY one tool card should land — not one per heredoc line
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1, (
        f"expected 1 tool card, got {len(tool_msgs)}: "
        f"{[m.tool_card.raw_command[:40] for m in tool_msgs]}"
    )

    # The card's raw command should be the WHOLE heredoc, preserving
    # the multi-line structure
    raw = tool_msgs[0].tool_card.raw_command
    assert "<!DOCTYPE html>" in raw
    assert raw.endswith("EOF")

    # And the file should actually exist with the right content
    assert target.exists()
    content = target.read_text()
    assert "<!DOCTYPE html>" in content
    assert "Hello from Successor" in content
    assert "</html>" in content


def test_multi_line_script_dispatches_as_one_block(tmp_path) -> None:
    """A block with several sequential commands (cd; ls; pwd) lands
    as ONE tool card containing the whole script, run via bash."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="multi",
        tool_config={"bash": {"allow_dangerous": True}},
    )
    chat.messages = []
    chat._stream_bash_detector = BashStreamDetector()
    chat._stream = _FakeStream([
        _content("```bash\necho one\necho two\necho three\n```\n"),
        _stream_end(),
    ])
    chat._pump_stream()
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    # All three echoes in the output
    output = tool_msgs[0].tool_card.output
    assert "one" in output
    assert "two" in output
    assert "three" in output


def test_read_only_profile_blocks_mutating_command_in_stream() -> None:
    """With allow_mutating=False, mkdir/touch/etc. are refused and
    the refusal hint points at the config flag."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="readonly",
        tool_config={"bash": {"allow_mutating": False}},
    )
    chat.messages = []
    chat._stream_bash_detector = BashStreamDetector()
    chat._stream = _FakeStream([
        _content("```bash\nmkdir /tmp/nope-successor\n```\n"),
        _stream_end(),
    ])
    chat._pump_stream()
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    # The refused card is the parse-only card — no exit code
    assert not tool_msgs[0].tool_card.executed
    assert tool_msgs[0].tool_card.risk == "mutating"
    # And the refusal hint points at the config path
    synthetic = [m for m in chat.messages if m.synthetic and "refused" in m.raw_text.lower()]
    assert any("read-only" in m.raw_text or "allow_mutating" in m.raw_text for m in synthetic)


# ─── Agent-loop continuation ───
#
# These tests cover the continue-loop: after a successful tool batch,
# _pump_stream calls _begin_agent_turn() again so the model can react
# to its own output. The MAX_AGENT_TURNS cap bounds runaway loops,
# and all-refused batches break the loop (the user has to resolve).


class _MockClient:
    """Multi-turn client stub: stream_chat pops from a preloaded
    queue of _FakeStream instances, one per expected turn.

    Tests install this on `chat.client` BEFORE calling `_submit`.
    Each subsequent `_begin_agent_turn` consumes one canned stream.
    If the queue is empty when `stream_chat` is called, the test
    intentionally failed to queue enough turns — raise loudly so
    the test author notices.
    """

    def __init__(self, streams: list) -> None:
        self._streams = list(streams)
        self.call_count = 0

    def stream_chat(self, messages, **kwargs):
        self.call_count += 1
        if not self._streams:
            raise RuntimeError(
                f"_MockClient exhausted on call #{self.call_count}: "
                f"test did not queue enough canned streams"
            )
        return self._streams.pop(0)


def _drive_until_idle(chat: SuccessorChat, max_ticks: int = 100) -> int:
    """Drive chat._pump_stream until the chat is idle (no stream OR
    no pending agent turn). Returns the number of ticks consumed.

    Hard-capped at `max_ticks` to catch runaway loops in tests.
    """
    for tick in range(max_ticks):
        if chat._stream is None and chat._agent_turn == 0:
            return tick
        chat._pump_stream()
    raise AssertionError(
        f"_drive_until_idle exceeded {max_ticks} ticks — loop did not settle"
    )


def test_continue_loop_runs_second_turn_after_tool(temp_config_dir: Path) -> None:
    """The archetype: turn 1 has bash + text, dispatch runs, turn 2
    is text-only ("I wrote the file, here's what it does"). The chat
    should commit both assistant messages, exactly one tool card in
    between, and end with _agent_turn reset to 0.
    """
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []
    chat.client = _MockClient(streams=[
        _FakeStream([
            _content("Let me check that for you.\n"),
            _content("```bash\necho first-turn-output\n```\n"),
            _stream_end(),
        ]),
        _FakeStream([
            _content("I ran the command and it echoed successfully.\n"),
            _stream_end(),
        ]),
    ])

    chat.input_buffer = "show me that thing"
    chat._submit()
    _drive_until_idle(chat)

    # Both streams were consumed
    assert chat.client.call_count == 2, (
        f"expected 2 stream_chat calls, got {chat.client.call_count}"
    )

    # Exactly one tool card
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    assert "first-turn-output" in tool_msgs[0].tool_card.output

    # Both assistant messages are present, in order
    assistant_texts = [
        m.raw_text for m in chat.messages
        if m.role == "successor" and not m.synthetic
    ]
    assert "Let me check that for you." in assistant_texts[0]
    # Second turn's commentary appears
    assert any("ran the command" in t for t in assistant_texts), (
        f"second-turn commentary missing from {assistant_texts}"
    )

    # Counter reset after settling
    assert chat._agent_turn == 0


def test_continue_loop_respects_turn_cap(temp_config_dir: Path) -> None:
    """If the model keeps emitting bash blocks turn after turn, the
    harness bails out at MAX_AGENT_TURNS with a visible marker.
    """
    from successor.chat import MAX_AGENT_TURNS
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []
    # Queue MANY more streams than MAX_AGENT_TURNS would consume. Each
    # one contains a simple echo so the dispatch succeeds and the loop
    # wants to keep going.
    chat.client = _MockClient(streams=[
        _FakeStream([
            _content(f"```bash\necho turn-{i}\n```\n"),
            _stream_end(),
        ])
        for i in range(MAX_AGENT_TURNS + 10)
    ])

    chat.input_buffer = "keep going"
    chat._submit()
    _drive_until_idle(chat, max_ticks=200)

    # Exactly MAX_AGENT_TURNS streams were consumed (not more)
    assert chat.client.call_count == MAX_AGENT_TURNS, (
        f"expected {MAX_AGENT_TURNS} stream_chat calls, "
        f"got {chat.client.call_count}"
    )

    # MAX_AGENT_TURNS tool cards were created
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == MAX_AGENT_TURNS

    # The halt marker is present
    halt_msgs = [
        m for m in chat.messages
        if m.synthetic and "halted" in m.raw_text.lower()
    ]
    assert len(halt_msgs) >= 1, "expected an 'agent loop halted' marker"


def test_continue_loop_skips_when_all_blocks_refused(temp_config_dir: Path) -> None:
    """If every block in a turn is refused, the loop does NOT continue
    — the user has to resolve before more commands make sense.
    """
    from successor.profiles import Profile

    chat = SuccessorChat()
    # Read-only profile → mutating commands get refused
    chat.profile = Profile(
        name="readonly",
        tools=("bash",),
        tool_config={"bash": {"allow_mutating": False}},
    )
    chat.messages = []
    chat.client = _MockClient(streams=[
        _FakeStream([
            _content("```bash\nmkdir /tmp/nope-successor-refused\n```\n"),
            _stream_end(),
        ]),
        # Turn 2 is deliberately NOT queued: if the loop tries to
        # continue, the mock will raise and the test will fail loudly.
    ])

    chat.input_buffer = "try to make a dir"
    chat._submit()
    _drive_until_idle(chat)

    # Only ONE stream consumed (no continuation)
    assert chat.client.call_count == 1

    # Refused card + refusal hint present
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    assert not tool_msgs[0].tool_card.executed
    assert tool_msgs[0].tool_card.risk == "mutating"

    # Counter reset
    assert chat._agent_turn == 0


def test_cwd_always_injected_into_system_prompt(temp_config_dir: Path) -> None:
    """The system prompt MUST include the effective cwd even when the
    profile doesn't pin `tool_config["bash"]["working_directory"]`.
    Regression guard for the P3 gap.
    """
    import os
    from successor.profiles import Profile

    chat = SuccessorChat()
    # Default-ish profile: bash enabled, NO working_directory pinned
    chat.profile = Profile(
        name="nodir",
        tools=("bash",),
        tool_config={},
    )
    chat.messages = []

    # Capture the payload on the only stream_chat call. We use a
    # MockClient that records the messages arg.
    captured: dict = {}
    class _CapturingClient:
        def stream_chat(self, messages, **kwargs):
            captured["messages"] = messages
            return _FakeStream([_stream_end()])
    chat.client = _CapturingClient()

    chat.input_buffer = "hi"
    chat._submit()
    _drive_until_idle(chat)

    sys_msg = captured["messages"][0]
    assert sys_msg["role"] == "system"
    # The actual process cwd is injected — not a placeholder, not empty
    assert "Bash working directory" in sys_msg["content"]
    assert f"cwd={os.getcwd()}" in sys_msg["content"]


def test_cwd_profile_override_takes_precedence(temp_config_dir: Path, tmp_path) -> None:
    """When the profile pins working_directory, that path is injected,
    not the process cwd.
    """
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="pinned",
        tools=("bash",),
        tool_config={"bash": {"working_directory": str(tmp_path)}},
    )
    chat.messages = []

    captured: dict = {}
    class _CapturingClient:
        def stream_chat(self, messages, **kwargs):
            captured["messages"] = messages
            return _FakeStream([_stream_end()])
    chat.client = _CapturingClient()

    chat.input_buffer = "hi"
    chat._submit()
    _drive_until_idle(chat)

    sys_msg = captured["messages"][0]
    assert f"cwd={tmp_path}" in sys_msg["content"]


def test_live_stream_never_exposes_bash_block_mid_stream(
    temp_config_dir: Path,
) -> None:
    """The in-flight streaming renderer reads from the bash detector's
    cleaned_text(), so the user NEVER sees raw fence markers or block
    content appearing in the assistant body. Fix for the 'spastic
    streaming' UX regression.
    """
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []

    # Install a stream that includes a bash fence mid-stream. We'll
    # drain it chunk-by-chunk and capture the visible stream text
    # at each step.
    fake = _FakeStream([
        _content("I'll run that:\n"),
        _content("```bash\n"),
        _content("echo hello\n"),
        _content("```\n"),
        _content("Done.\n"),
        _stream_end(),
    ])

    # Mock the client so _begin_agent_turn returns our fake for turn 1
    # and a text-only finalizer for turn 2 (continuation).
    chat.client = _MockClient(streams=[
        fake,
        _FakeStream([
            _content("The echo succeeded.\n"),
            _stream_end(),
        ]),
    ])

    chat.input_buffer = "run echo"
    chat._submit()

    # Single-tick observation: drain events one at a time (not all at
    # once via _drive_until_idle) so we can inspect the intermediate
    # state between ContentChunk deliveries. We do this by draining
    # the queue manually one event at a time.
    observed_visible = []
    orig_drain = fake.drain

    def one_at_a_time() -> list:
        if not fake._events:
            return []
        # Pop a single event and return it
        ev = fake._events.pop(0)
        return [ev]

    fake.drain = one_at_a_time  # type: ignore[method-assign]

    # Pump ticks until the first fake stream is fully drained
    safety = 0
    while fake._events and safety < 20:
        chat._pump_stream()
        if chat._stream_bash_detector is not None:
            observed_visible.append(
                chat._stream_bash_detector.cleaned_text()
            )
        safety += 1

    # At NO POINT should the raw fence OR the raw command body have
    # appeared in the visible cleaned text
    for snap in observed_visible:
        assert "```" not in snap, f"fence marker leaked into visible: {snap!r}"
        assert "echo hello" not in snap, (
            f"bash block content leaked into visible: {snap!r}"
        )

    # But the surrounding prose IS visible
    joined = "\n".join(observed_visible)
    assert "I'll run that" in joined, "prose before the block was elided"
