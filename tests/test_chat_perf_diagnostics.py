from __future__ import annotations

import json
from pathlib import Path

import pytest

import successor.chat as chat_module
from successor.chat import SuccessorChat
from successor.context_usage import TurnRequestEnvelope, build_stream_perf_snapshot
from successor.providers.llama import StreamEnded


class _FakeStream:
    def __init__(self, events: list[object]) -> None:
        self._events = list(events)

    def drain(self) -> list[object]:
        out = list(self._events)
        self._events = []
        return out

    def close(self) -> None:
        pass


def _trace_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_stream_end_trace_records_perf_and_llama_timings(
    temp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat._agent_turn = 4
    chat._active_request_envelope = TurnRequestEnvelope(
        turn=4,
        system_sections=(),
        system_prompt="",
        api_messages=(),
        request_messages=(),
        tool_schemas=(),
        enabled_tools=(),
        enabled_skills=(),
        stable_system_hash="stablehash123",
        request_slot_id=0,
        request_cache_prompt=True,
    )
    chat._stream_opened_at = 10.0
    chat._stream_started_at = 12.5
    chat._stream_content = ["hello"]
    chat._stream = _FakeStream([
        StreamEnded(
            finish_reason="stop",
            usage={
                "prompt_tokens": 1600,
                "completion_tokens": 12,
                "total_tokens": 1612,
            },
            timings={
                "cache_n": 1536,
                "prompt_n": 64,
                "prompt_ms": 120.0,
                "predicted_n": 12,
                "predicted_ms": 80.0,
            },
        )
    ])
    monkeypatch.setattr(chat_module.time, "monotonic", lambda: 16.0)

    chat._pump_stream()
    chat._trace.close()

    events = _trace_events(chat.session_trace_path)
    stream_end = next(event for event in events if event.get("type") == "stream_end")
    assert stream_end["first_token_ms"] == pytest.approx(2500.0)
    assert stream_end["total_stream_ms"] == pytest.approx(6000.0)
    assert stream_end["provider_timings"]["cache_n"] == 1536
    assert stream_end["provider_timings"]["prompt_n"] == 64
    assert stream_end["prompt_cache_hit_ratio"] == pytest.approx(0.96)
    assert stream_end["suspected_kv_miss"] is False

    assert chat._last_stream_perf is not None
    assert chat._last_stream_perf.request_slot_id == 0
    assert chat._last_stream_perf.cache_hit_tokens == 1536
    assert chat._last_stream_perf.prompt_eval_tokens == 64
    assert chat._last_stream_perf.prompt_cache_hit_ratio == pytest.approx(0.96)


def test_perf_command_reports_recent_turns_and_suspected_kv_miss(
    temp_config_dir: Path,
) -> None:
    chat = SuccessorChat()
    chat.messages = []

    prior = build_stream_perf_snapshot(
        turn=2,
        finish_reason="stop",
        provider="llamacpp",
        stable_system_hash="stablehash123",
        request_slot_id=0,
        request_cache_prompt=True,
        raw_usage={"prompt_tokens": 1800, "completion_tokens": 10, "total_tokens": 1810},
        raw_timings={
            "cache_n": 1500,
            "prompt_n": 300,
            "prompt_ms": 200.0,
            "predicted_n": 10,
            "predicted_ms": 90.0,
        },
    )
    latest = build_stream_perf_snapshot(
        turn=3,
        finish_reason="stop",
        provider="llamacpp",
        stable_system_hash="stablehash123",
        request_slot_id=0,
        request_cache_prompt=True,
        raw_usage={"prompt_tokens": 1600, "completion_tokens": 14, "total_tokens": 1614},
        raw_timings={
            "cache_n": 0,
            "prompt_n": 1600,
            "prompt_ms": 1400.0,
            "predicted_n": 14,
            "predicted_ms": 110.0,
        },
        prior_snapshots=(prior,),
    )
    chat._recent_stream_perf = [prior, latest]
    chat._last_stream_perf = latest

    chat.input_buffer = "/kv"
    chat._submit()

    assert chat.messages, "expected /kv to append a synthetic report"
    report = chat.messages[-1].raw_text
    assert "last turn 3" in report
    assert "cache_n=0" in report
    assert "suspected KV miss" in report
    assert "recent turns:" in report
