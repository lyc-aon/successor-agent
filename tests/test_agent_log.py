"""Tests for agent/log.py — message log data shapes."""

from __future__ import annotations

from successor.agent.log import (
    ApiRound,
    AttachmentRegistry,
    BoundaryMarker,
    LogMessage,
    MessageLog,
)
from successor.bash import dispatch_bash


# ─── LogMessage ───


def test_log_message_basic() -> None:
    m = LogMessage(role="user", content="hello", created_at=1.0)
    assert m.role == "user"
    assert m.content == "hello"
    assert m.tool_card is None
    assert not m.is_summary
    assert not m.is_boundary


def test_log_message_to_api_dict_plain() -> None:
    m = LogMessage(role="assistant", content="hi there")
    d = m.to_api_dict()
    assert d == {"role": "assistant", "content": "hi there"}


def test_log_message_to_api_dict_with_tool_card() -> None:
    """Tool messages serialize as assistant messages with the raw
    command + output joined into a single content string."""
    card = dispatch_bash("echo hello")
    m = LogMessage(role="tool", content="", tool_card=card)
    d = m.to_api_dict()
    assert d["role"] == "assistant"
    assert "$ echo hello" in d["content"]
    assert "hello" in d["content"]


def test_log_message_replace_output() -> None:
    """replace_output() returns a NEW message with the tool card's
    output replaced — original is untouched."""
    card = dispatch_bash("echo original")
    m = LogMessage(role="tool", content="", tool_card=card)
    m2 = m.replace_output("[cleared]")
    assert m.tool_card.output != m2.tool_card.output
    assert m2.tool_card.output == "[cleared]"


def test_log_message_replace_output_no_card_returns_self() -> None:
    m = LogMessage(role="user", content="hi")
    assert m.replace_output("anything") is m


# ─── BoundaryMarker ───


def test_boundary_reduction_pct() -> None:
    b = BoundaryMarker(
        happened_at=0.0,
        pre_compact_tokens=1000,
        post_compact_tokens=200,
        rounds_summarized=10,
        summary_text="x",
    )
    assert b.reduction_pct == 80.0


def test_boundary_reduction_pct_zero_pre() -> None:
    b = BoundaryMarker(
        happened_at=0.0, pre_compact_tokens=0,
        post_compact_tokens=0, rounds_summarized=0, summary_text="x",
    )
    assert b.reduction_pct == 0.0


# ─── ApiRound ───


def test_api_round_append_and_count() -> None:
    r = ApiRound()
    r.append(LogMessage(role="user", content="abc"))
    r.append(LogMessage(role="assistant", content="defg"))
    assert len(r.messages) == 2
    assert r.char_count() == 7


def test_api_round_first_user_text() -> None:
    r = ApiRound()
    r.append(LogMessage(role="assistant", content="hi"))
    r.append(LogMessage(role="user", content="actual user msg"))
    assert r.first_user_text == "actual user msg"


def test_api_round_text_for_tokenizing_includes_tool_output() -> None:
    r = ApiRound()
    card = dispatch_bash("echo concat-test")
    r.append(LogMessage(role="user", content="run echo"))
    r.append(LogMessage(role="tool", content="", tool_card=card))
    text = r.text_for_tokenizing()
    assert "run echo" in text
    assert "echo concat-test" in text
    assert "concat-test" in text


# ─── MessageLog ───


def test_message_log_starts_empty() -> None:
    log = MessageLog()
    assert log.is_empty()
    assert log.round_count == 0
    assert log.total_messages() == 0
    assert log.latest_round is None


def test_message_log_begin_round_appends() -> None:
    log = MessageLog()
    r = log.begin_round()
    assert log.round_count == 1
    assert log.latest_round is r


def test_append_to_current_round_creates_round_if_needed() -> None:
    log = MessageLog()
    log.append_to_current_round(LogMessage(role="user", content="hi"))
    assert log.round_count == 1
    assert log.total_messages() == 1


def test_append_attaches_paths_to_attachment_registry() -> None:
    log = MessageLog()
    card = dispatch_bash("cat README.md")
    log.append_to_current_round(LogMessage(role="tool", content="", tool_card=card))
    assert "README.md" in log.attachments.files


def test_attachment_registry_recent_orders_by_time() -> None:
    reg = AttachmentRegistry()
    reg.note("a", at=1.0)
    reg.note("b", at=3.0)
    reg.note("c", at=2.0)
    recent = reg.recent(n=10)
    assert recent == ["b", "c", "a"]


def test_attachment_registry_recent_truncates() -> None:
    reg = AttachmentRegistry()
    for i in range(10):
        reg.note(f"file{i}", at=float(i))
    assert len(reg.recent(n=3)) == 3


def test_message_log_api_messages_includes_system_prompt() -> None:
    log = MessageLog(system_prompt="You are successor.")
    log.append_to_current_round(LogMessage(role="user", content="hi"))
    msgs = log.api_messages()
    assert msgs[0] == {"role": "system", "content": "You are successor."}
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_message_log_api_messages_no_system_prompt() -> None:
    log = MessageLog()  # no system_prompt
    log.append_to_current_round(LogMessage(role="user", content="hi"))
    msgs = log.api_messages()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_message_log_truncate_oldest_round() -> None:
    log = MessageLog()
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="r1"))
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="r2"))
    dropped = log.truncate_oldest_round()
    assert dropped is not None
    assert dropped.messages[0].content == "r1"
    assert log.round_count == 1


def test_message_log_insert_boundary_creates_two_rounds() -> None:
    """A boundary insertion adds [boundary_round, summary_round] at
    the position. The boundary marker round contains a single
    is_boundary message; the summary round contains the summary text."""
    log = MessageLog()
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="r1"))
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="r2"))

    b = BoundaryMarker(
        happened_at=10.0, pre_compact_tokens=500,
        post_compact_tokens=80, rounds_summarized=2,
        summary_text="conversation summary here",
    )
    log.insert_boundary(b, b.summary_text, position=0)

    assert log.round_count == 4  # 2 original + 2 inserted
    boundaries = log.boundaries()
    assert len(boundaries) == 1
    assert "compaction" in boundaries[0].content
    # The next round is the summary
    assert log.rounds[1].messages[0].is_summary


def test_message_log_iter_messages_walks_in_order() -> None:
    log = MessageLog()
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="a"))
    log.append_to_current_round(LogMessage(role="assistant", content="b"))
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="c"))
    contents = [m.content for m in log.iter_messages()]
    assert contents == ["a", "b", "c"]
