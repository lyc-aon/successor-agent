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

import json
import time
from pathlib import Path


from successor.agent.bash_stream import BashStreamDetector
from successor.bash import dispatch_bash, preview_bash
from successor.chat import SuccessorChat, _Message
from successor.providers.llama import (
    ContentChunk,
    LlamaCppRuntimeCapabilities,
    StreamEnded,
)
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


def _drain_runners(chat: SuccessorChat, max_ticks: int = 500) -> None:
    """Pump _pump_running_tools until every in-flight bash runner has
    completed and its preview card has been replaced with the final
    enriched ToolCard. Used by tests that dispatch via /bash or via
    _pump_stream and need to wait for the subprocess to finish before
    asserting on card.exit_code / card.output / etc.
    """
    import time as _time
    for _ in range(max_ticks):
        if not chat._running_tools:
            return
        chat._pump_running_tools()
        _time.sleep(0.005)
    raise AssertionError(
        f"_drain_runners exceeded {max_ticks} ticks — runners did not finish"
    )


# ─── /bash slash command ───


def test_bash_command_appends_tool_message(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash echo hello"
    chat._submit()
    _drain_runners(chat)

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
    _drain_runners(chat)
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
    _drain_runners(chat)

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
    _drain_runners(chat)
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


def _drive_until_idle(
    chat: SuccessorChat,
    max_ticks: int = 1000,
    *,
    tick_sleep_s: float = 0.005,
) -> int:
    """Drive chat._pump_stream + chat._pump_running_tools until the
    chat is fully idle: no stream in flight, no pending agent turn,
    AND no in-flight bash runners. Returns the number of ticks
    consumed.

    Hard-capped at `max_ticks`. By default the helper sleeps 5ms
    between ticks to give real subprocess-backed runners time to make
    progress, but fully mocked tests can set `tick_sleep_s=0` to drive
    the loop without wall-clock delay.
    """
    import time as _time
    for tick in range(max_ticks):
        if (
            chat._stream is None
            and chat._agent_turn == 0
            and not chat._running_tools
        ):
            return tick
        chat._pump_stream()
        chat._pump_running_tools()
        if tick_sleep_s > 0:
            _time.sleep(tick_sleep_s)
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

    loop_cap = min(MAX_AGENT_TURNS, 12)

    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
        max_agent_turns=loop_cap,
    )
    chat.messages = []
    # Queue MANY more streams than the enforced cap would consume. Each
    # one contains a simple echo so the dispatch succeeds and the loop
    # wants to keep going.
    chat.client = _MockClient(streams=[
        _FakeStream([
            _content(f"```bash\necho turn-{i}\n```\n"),
            _stream_end(),
        ])
        for i in range(loop_cap + 10)
    ])

    chat.input_buffer = "keep going"
    chat._submit()
    _drive_until_idle(chat, max_ticks=max(1000, loop_cap * 8))

    # Exactly the enforced cap was consumed (not more).
    assert chat.client.call_count == loop_cap, (
        f"expected {loop_cap} stream_chat calls, "
        f"got {chat.client.call_count}"
    )

    # One tool card per turn.
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == loop_cap

    # The halt marker is present and names the enforced cap.
    halt_msgs = [
        m for m in chat.messages
        if m.synthetic and f"halted at {loop_cap} turns" in (m.raw_text or "").lower()
    ]
    assert halt_msgs, "expected an 'agent loop halted' marker"


def test_continue_loop_respects_profile_max_agent_turns(temp_config_dir: Path) -> None:
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="short-cap",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
        max_agent_turns=7,
    )
    chat.messages = []
    chat.client = _MockClient(streams=[
        _FakeStream([
            _content(f"```bash\necho turn-{i}\n```\n"),
            _stream_end(),
        ])
        for i in range(20)
    ])

    chat.input_buffer = "keep going"
    chat._submit()
    _drive_until_idle(chat, max_ticks=200)

    assert chat.client.call_count == 7
    halt_msgs = [
        m for m in chat.messages
        if m.synthetic and "halted at 7 turns" in (m.raw_text or "").lower()
    ]
    assert halt_msgs, "expected the custom turn cap to appear in the halt marker"


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
    assert "Working directory" in sys_msg["content"]
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


def test_parallel_read_guidance_only_injected_when_provider_supports_it(
    temp_config_dir: Path,
) -> None:
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="parallel-reads",
        tools=("bash",),
        tool_config={},
    )
    chat.messages = []

    captured: dict = {}

    class _CapturingClient:
        def stream_chat(self, messages, **kwargs):
            captured["messages"] = messages
            return _FakeStream([_stream_end()])

        def detect_runtime_capabilities(self):
            return LlamaCppRuntimeCapabilities(
                context_window=262144,
                total_slots=4,
                endpoint_slots=True,
                supports_parallel_tool_calls=True,
            )

    chat.client = _CapturingClient()
    chat.input_buffer = "inspect two files"
    chat._submit()
    _drive_until_idle(chat)

    sys_msg = captured["messages"][0]
    assert "Parallel tool calls" in sys_msg["content"]
    assert "multiple `read_file` calls, read-only `bash` checks" in sys_msg["content"]


def test_execution_discipline_injected_when_tools_enabled(
    temp_config_dir: Path,
) -> None:
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="discipline",
        tools=("read_file", "bash"),
        tool_config={},
    )
    chat.messages = []

    captured: dict = {}

    class _CapturingClient:
        def stream_chat(self, messages, **kwargs):
            captured["messages"] = messages
            return _FakeStream([_stream_end()])

    chat.client = _CapturingClient()
    chat.input_buffer = "inspect and verify"
    chat._submit()
    _drive_until_idle(chat)

    sys_msg = captured["messages"][0]
    assert "Execution discipline" in sys_msg["content"]
    assert "Do not stop early when another tool call would materially improve the result." in sys_msg["content"]
    assert "make the corresponding tool call in the SAME response" in sys_msg["content"]
    assert "Task-ledger discipline" in sys_msg["content"]
    assert "Evidence-bearing verification" in sys_msg["content"]
    assert "Experimental run discipline" in sys_msg["content"]
    assert "No current task ledger." in sys_msg["content"]
    assert "No current verification contract." in sys_msg["content"]
    assert "No current runbook." in sys_msg["content"]
    assert "Working with local files" in sys_msg["content"]
    assert sys_msg["content"].index("Task-ledger discipline") < sys_msg["content"].index("Working with local files")
    assert sys_msg["content"].index("## Current Session Tasks") < sys_msg["content"].index("Working with local files")
    assert sys_msg["content"].index("## Current Verification Contract") < sys_msg["content"].index("Working with local files")
    assert sys_msg["content"].index("## Current Runbook") < sys_msg["content"].index("Working with local files")


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


# ─── Native tool_calls path (Qwen 3.5 structured tool calling) ───
#
# These tests cover the Qwen-native tool_calls flow added in the
# 2026-04-07 architectural fix. Provider parses streamed tool_calls
# into StreamEnded.tool_calls; chat dispatches them via
# _dispatch_native_tool_calls; api_messages serializer round-trips
# them as `tool_calls` on assistant messages with `tool_call_id`
# linking on tool messages. The chat template renders this as
# native `<tool_call>` and `<tool_response>` blocks.


def _native_tool_call_stream(
    command: str,
    call_id: str = "call_test_0",
    pre_text: str = "",
):
    """Build a fake stream that delivers a single native bash tool_call.

    Mirrors what llama.cpp's SSE parser feeds the chat: optional content
    text, then a StreamEnded with `tool_calls` populated. Used by
    multiple tests below.
    """
    events: list = []
    if pre_text:
        events.append(_content(pre_text))
    events.append(StreamEnded(
        finish_reason="tool_calls",
        usage=None,
        timings=None,
        full_reasoning="",
        full_content=pre_text,
        tool_calls=({
            "id": call_id,
            "name": "bash",
            "arguments": {"command": command},
            "raw_arguments": '{"command": "' + command + '"}',
        },),
    ))
    return _FakeStream(events)


def test_native_tool_call_dispatches_via_pump(temp_config_dir: Path) -> None:
    """A StreamEnded carrying tool_calls should produce one tool card
    per call, dispatched through dispatch_bash, with the model's id
    propagated onto the card."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []
    chat._stream_bash_detector = None  # native path only
    chat._stream = _native_tool_call_stream(
        "echo from-native-call",
        call_id="call_native_42",
    )
    chat._pump_stream()
    _drain_runners(chat)

    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    card = tool_msgs[0].tool_card
    assert card.exit_code == 0
    assert "from-native-call" in card.output
    assert card.tool_call_id == "call_native_42"


def test_native_tool_call_unknown_function_skipped(temp_config_dir: Path) -> None:
    """Tool calls with a name other than 'bash' should NOT dispatch
    and should produce a synthetic error message instead."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []
    chat._stream_bash_detector = None
    chat._stream = _FakeStream([
        StreamEnded(
            finish_reason="tool_calls",
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_x",
                "name": "ls",
                "arguments": {"path": "/tmp"},
                "raw_arguments": '{"path": "/tmp"}',
            },),
        ),
    ])
    chat._pump_stream()

    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert tool_msgs == []
    error_notes = [
        m for m in chat.messages
        if m.synthetic and "unknown tool" in m.raw_text.lower()
    ]
    assert len(error_notes) == 1


def test_api_messages_emits_tool_calls_for_assistant_with_cards(
    temp_config_dir: Path,
) -> None:
    """When the chat builds api_messages from a history that has an
    assistant message followed by tool cards, it should produce ONE
    assistant message with `tool_calls` populated and one `role: tool`
    message per card with `tool_call_id` linking back."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )

    # Build a synthetic history: user → empty assistant marker → tool card
    chat.messages = [
        _Message("user", "list things"),
        _Message("successor", "", display_text=""),
        _Message(
            "tool", "",
            tool_card=dispatch_bash("echo hi", tool_call_id="call_abc123"),
        ),
    ]

    api_messages = chat._build_api_messages_native("SYS")

    # Walk and find the assistant + tool pair
    roles = [m["role"] for m in api_messages]
    assert "system" in roles
    assert "user" in roles
    assert "assistant" in roles
    assert "tool" in roles

    asst_msgs = [m for m in api_messages if m["role"] == "assistant"]
    assert len(asst_msgs) == 1
    assert "tool_calls" in asst_msgs[0]
    assert len(asst_msgs[0]["tool_calls"]) == 1
    tc = asst_msgs[0]["tool_calls"][0]
    assert tc["id"] == "call_abc123"
    assert tc["function"]["name"] == "bash"

    tool_role_msgs = [m for m in api_messages if m["role"] == "tool"]
    assert len(tool_role_msgs) == 1
    assert tool_role_msgs[0]["tool_call_id"] == "call_abc123"


def test_api_messages_separate_turns_keep_distinct_tool_calls(
    temp_config_dir: Path,
) -> None:
    """When two separate agent turns each emit a tool call, the api
    messages must have TWO distinct (assistant, tool) pairs — not one
    grouped pair. Otherwise the model thinks it made parallel calls
    in a single turn and re-issues them."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )

    chat.messages = [
        _Message("user", "do two things"),
        _Message("successor", "", display_text=""),  # turn 1 marker
        _Message(
            "tool", "",
            tool_card=dispatch_bash("echo first", tool_call_id="call_1"),
        ),
        _Message("successor", "", display_text=""),  # turn 2 marker
        _Message(
            "tool", "",
            tool_card=dispatch_bash("echo second", tool_call_id="call_2"),
        ),
    ]

    api_messages = chat._build_api_messages_native("SYS")

    asst_msgs = [m for m in api_messages if m["role"] == "assistant"]
    assert len(asst_msgs) == 2, (
        f"expected 2 distinct assistant turns, got {len(asst_msgs)}"
    )
    # Each assistant turn carries exactly its OWN single tool call
    assert asst_msgs[0]["tool_calls"][0]["id"] == "call_1"
    assert asst_msgs[1]["tool_calls"][0]["id"] == "call_2"

    tool_role_msgs = [m for m in api_messages if m["role"] == "tool"]
    assert len(tool_role_msgs) == 2
    assert tool_role_msgs[0]["tool_call_id"] == "call_1"
    assert tool_role_msgs[1]["tool_call_id"] == "call_2"


def test_api_messages_orphan_tool_card_synthesizes_assistant(
    temp_config_dir: Path,
) -> None:
    """A tool card without a preceding assistant marker (e.g., from
    the /bash slash command echo) should still get an assistant turn
    synthesized so the tool result has a tool_call to link back to."""
    from successor.profiles import Profile
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )

    chat.messages = [
        _Message(
            "tool", "",
            tool_card=dispatch_bash("echo orphan", tool_call_id="call_orphan"),
        ),
    ]

    api_messages = chat._build_api_messages_native("SYS")

    asst_msgs = [m for m in api_messages if m["role"] == "assistant"]
    assert len(asst_msgs) == 1
    assert asst_msgs[0]["tool_calls"][0]["id"] == "call_orphan"

    tool_role_msgs = [m for m in api_messages if m["role"] == "tool"]
    assert len(tool_role_msgs) == 1
    assert tool_role_msgs[0]["tool_call_id"] == "call_orphan"


def test_dispatch_bash_assigns_synthetic_call_id_when_omitted() -> None:
    """dispatch_bash should always populate tool_call_id on the
    returned card, generating a synthetic uuid when the caller didn't
    provide one (legacy bash detector path, /bash slash command,
    direct test invocations)."""
    card = dispatch_bash("echo hi")
    assert card.tool_call_id
    assert card.tool_call_id.startswith("call_")
    # Two consecutive dispatches must produce DIFFERENT ids
    card2 = dispatch_bash("echo hi")
    assert card.tool_call_id != card2.tool_call_id


def test_dispatch_bash_propagates_provided_call_id() -> None:
    """When the chat passes a model-provided id (from a streamed
    tool_calls block), dispatch_bash should use it verbatim instead
    of generating a fresh one."""
    card = dispatch_bash("echo hi", tool_call_id="call_from_model")
    assert card.tool_call_id == "call_from_model"


def test_provider_finalizes_streamed_tool_call_args() -> None:
    """The provider's _finalize_tool_calls helper concatenates
    fragmented arguments JSON and parses it. This is the unit-level
    check that the streaming protocol → structured StreamEnded.tool_calls
    transformation works."""
    from successor.providers.llama import ChatStream
    pending = {
        0: {
            "id": "call_xyz",
            "name": "bash",
            "args_buf": ['{"comm', 'and": "ls', ' -la"}'],
        },
    }
    final = ChatStream._finalize_tool_calls(pending)
    assert len(final) == 1
    tc = final[0]
    assert tc["id"] == "call_xyz"
    assert tc["name"] == "bash"
    assert tc["arguments"] == {"command": "ls -la"}
    assert tc["raw_arguments"] == '{"command": "ls -la"}'


def test_provider_finalize_handles_invalid_json_gracefully() -> None:
    """If the model produced malformed JSON in arguments, the parser
    should NOT crash — return arguments={} and preserve raw_arguments
    for the consumer to inspect."""
    from successor.providers.llama import ChatStream
    pending = {
        0: {
            "id": "call_bad",
            "name": "bash",
            "args_buf": ['{"command": "ls'],  # missing close
        },
    }
    final = ChatStream._finalize_tool_calls(pending)
    assert len(final) == 1
    assert final[0]["arguments"] == {}
    assert final[0]["raw_arguments"] == '{"command": "ls'
    assert final[0]["arguments_parse_error"] == "Unterminated string starting at"
    assert final[0]["arguments_parse_error_pos"] == 12


def test_native_tool_call_invalid_json_reports_truncation_cleanly(
    temp_config_dir: Path,
) -> None:
    """Malformed native bash arguments should log a concise note
    instead of dumping the full raw payload into the transcript."""
    from successor.profiles import Profile

    raw = '{"command":"cat > styles.css << \'EOF\'\\n:root {\\n  --space-1: 4px;\\n  --space-2: 8px;'
    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []
    chat._stream_bash_detector = None
    chat._stream = _FakeStream([
        StreamEnded(
            finish_reason="length",
            finish_reason_reported=True,
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_bad",
                "name": "bash",
                "arguments": {},
                "raw_arguments": raw,
                "arguments_parse_error": "Unterminated string starting at",
                "arguments_parse_error_pos": 11,
            },),
        ),
    ])
    chat._pump_stream()

    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert tool_msgs == []
    notes = [
        m for m in chat.messages
        if m.synthetic and "malformed or truncated before dispatch" in m.raw_text
    ]
    assert len(notes) == 1
    note = notes[0].raw_text
    assert "finish_reason=length" in note
    assert "Retry with a smaller command" in note
    assert "{\"command\":\"cat > styles.css" in note
    assert len(note) < 500

    events = [
        json.loads(line)
        for line in chat.session_trace_path.read_text().splitlines()
        if line.strip()
    ]
    stream_end = next(ev for ev in events if ev.get("type") == "stream_end")
    assert stream_end["finish_reason"] == "length"
    assert stream_end["finish_reason_reported"] is True
    assert stream_end["native_tool_calls"][0]["arguments_parse_error"] == "Unterminated string starting at"


# ─── Async runner integration with the chat tick loop ───
#
# These tests cover the async dispatch path: BashRunner spawned per
# tool call, _Message.running_tool tracks the in-flight runner, the
# chat's _pump_running_tools polls each tick, and finalization
# replaces the preview card with the enriched card. Continuation
# stream fires only after the LAST runner in a batch completes.


def test_bash_dispatch_spawns_running_message_immediately(
    temp_config_dir: Path,
) -> None:
    """The /bash slash command spawns an async runner. Right after
    _submit() returns, before draining, the message exists with
    running_tool set and tool_card is the PREVIEW (no exit_code yet)."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash echo immediate"
    chat._submit()

    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    msg = tool_msgs[0]
    # The runner should still be in flight (or just barely done)
    assert msg.tool_card is not None
    assert msg.tool_card.verb == "print-text"
    # Until the runner finishes, exit_code is None on the preview
    if msg.running_tool is not None:
        assert msg.tool_card.exit_code is None
    assert chat._running_tools, "runner should be registered for polling"

    # Now drain — the runner should complete and the card finalize
    _drain_runners(chat)
    assert msg.running_tool is None
    assert msg.tool_card.exit_code == 0
    assert "immediate" in msg.tool_card.output
    assert chat._running_tools == []


def test_running_tool_pulse_does_not_block_tick(
    temp_config_dir: Path,
) -> None:
    """While a runner is in flight, repeated calls to on_tick must
    return promptly even though the subprocess is sleeping. Proves
    the tick loop is unblocked by async dispatch."""
    from successor.render.cells import Grid
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash sleep 0.15 && echo done"
    chat._submit()

    g = Grid(20, 80)
    tick_count = 0
    t0 = time.monotonic()
    while chat._running_tools and time.monotonic() - t0 < 1.0:
        chat.on_tick(g)
        tick_count += 1
    # Final drain in case the runner finished between iterations
    chat._pump_running_tools()
    elapsed = time.monotonic() - t0

    # Subprocess slept ~150ms; we should have ticked many times
    assert tick_count >= 10, (
        f"only {tick_count} ticks during {elapsed:.3f}s of subprocess "
        f"runtime — tick loop may be blocking on dispatch"
    )
    # The card finalized
    cards = [m for m in chat.messages if m.tool_card is not None]
    assert cards
    assert cards[0].tool_card.exit_code == 0


def test_running_tool_renders_with_spinner_glyph(
    temp_config_dir: Path,
) -> None:
    """While a runner is in flight, the chat paint should produce
    rows that DON'T match the static path. Specifically the running
    state uses a Braille spinner glyph instead of the verb-class
    glyph (▸ ✎ ⌕)."""
    from successor.render.cells import Grid
    from successor.snapshot import render_grid_to_plain
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash sleep 0.2"
    chat._submit()

    # Paint immediately while runner is in flight
    g = Grid(20, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    # Spinner frames are Braille chars from the canonical sequence.
    # At least one of them should appear in the rendered output.
    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert any(c in plain for c in spinner_chars), (
        f"expected a Braille spinner glyph in running card paint, "
        f"got: {plain[:500]}"
    )
    # Status footer should say "running"
    assert "running" in plain.lower()

    _drain_runners(chat)


def test_continuation_fires_only_after_last_runner_completes(
    temp_config_dir: Path,
) -> None:
    """When the model emits two tool calls in one stream, the
    continuation should fire only AFTER both runners complete, not
    after the first one. This is the batch-aware continue-loop."""
    from successor.profiles import Profile

    chat = SuccessorChat()
    chat.profile = Profile(
        name="yolo",
        tools=("bash",),
        tool_config={"bash": {"allow_dangerous": True, "allow_mutating": True}},
    )
    chat.messages = []

    # Mock client that returns two streams
    chat.client = _MockClient(streams=[
        # Turn 1: emit two tool calls in one stream
        _FakeStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=(
                    {
                        "id": "call_a",
                        "name": "bash",
                        "arguments": {"command": "echo a"},
                        "raw_arguments": '{"command": "echo a"}',
                    },
                    {
                        "id": "call_b",
                        "name": "bash",
                        "arguments": {"command": "echo b"},
                        "raw_arguments": '{"command": "echo b"}',
                    },
                ),
            ),
        ]),
        # Turn 2: text-only acknowledgment
        _FakeStream([
            _content("both done"),
            _stream_end(),
        ]),
    ])

    chat.input_buffer = "do two things"
    chat._submit()
    _drive_until_idle(chat)

    # Two cards landed, both executed
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 2
    assert all(m.tool_card.exit_code == 0 for m in tool_msgs)
    assert "a" in tool_msgs[0].tool_card.output
    assert "b" in tool_msgs[1].tool_card.output
    # Continuation produced the final assistant message
    final = [
        m for m in chat.messages
        if m.role == "successor" and not m.synthetic and "both done" in m.raw_text
    ]
    assert len(final) == 1
    # Both client calls were consumed
    assert chat.client.call_count == 2


def test_cancel_running_tools_terminates_in_flight_runner(
    temp_config_dir: Path,
) -> None:
    """_cancel_running_tools should kill any in-flight subprocess and
    surface its card with the cancellation marker in stderr."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash sleep 5"
    chat._submit()
    assert chat._running_tools

    chat._cancel_running_tools()
    _drain_runners(chat)

    cards = [m for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    final = cards[0].tool_card
    # Subprocess was killed, exit_code is the negative signal
    assert final.exit_code is not None
    assert final.exit_code < 0
    # Cancellation marker is preserved in stderr
    assert "cancelled" in final.stderr.lower()


def test_new_submit_cancels_previous_runners(
    temp_config_dir: Path,
) -> None:
    """If the user submits a new message while runners are still in
    flight from a previous turn, those runners should be cancelled
    so the new turn can start with a clean slate."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash sleep 5"
    chat._submit()
    assert chat._running_tools

    # User submits a new message — should cancel the running runner
    chat.input_buffer = "/bash echo new"
    chat._submit()

    _drain_runners(chat)

    cards = [m for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 2
    # First card was cancelled
    first = cards[0].tool_card
    assert first.exit_code is not None and first.exit_code < 0
    # Second card ran cleanly
    second = cards[1].tool_card
    assert second.exit_code == 0
    assert "new" in second.output
