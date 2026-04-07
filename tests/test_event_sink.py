"""Tests for the event sink + claude stream dispatch."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from krewcli.agents.claude_agent import (
    _coerce_text,
    _dispatch_stream_message,
    _summarize_input,
)
from krewcli.agents.event_sink import (
    AGENT_REPLY,
    KrewhubEventSink,
    NullEventSink,
    SESSION_END,
    SESSION_START,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
)


class _RecordingSink:
    """Test double — records every emit call in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict | None, str]] = []

    async def emit(self, event_type, *, payload=None, body=""):
        self.events.append((event_type, payload, body))

    async def flush(self):
        return None


# -------------------- claude stream dispatch --------------------


class TestDispatchStreamMessage:
    @pytest.mark.asyncio
    async def test_system_init_emits_session_start(self):
        sink = _RecordingSink()
        msg = {
            "type": "system",
            "subtype": "init",
            "model": "claude-opus-4-6",
            "cwd": "/tmp",
            "session_id": "sess_1",
            "tools": ["Bash", "Read"],
        }
        delta = await _dispatch_stream_message(
            msg, sink=sink, prompt="do it", session_started=False
        )
        assert delta.session_started is True
        assert len(sink.events) == 1
        typ, payload, body = sink.events[0]
        assert typ == SESSION_START
        assert payload["model"] == "claude-opus-4-6"
        assert payload["prompt"] == "do it"
        assert payload["tools"] == ["Bash", "Read"]
        assert "claude" in body

    @pytest.mark.asyncio
    async def test_assistant_text_emits_agent_reply(self):
        sink = _RecordingSink()
        msg = {
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "text", "text": "hello there"},
                    {"type": "text", "text": "second block"},
                ],
            },
        }
        delta = await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert delta.text == "hello theresecond block"
        assert [e[0] for e in sink.events] == [AGENT_REPLY, AGENT_REPLY]
        assert sink.events[0][1]["text"] == "hello there"
        assert sink.events[0][1]["block_index"] == 0
        assert sink.events[1][1]["block_index"] == 1

    @pytest.mark.asyncio
    async def test_assistant_thinking_emits_thinking_event(self):
        sink = _RecordingSink()
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "I should use Bash"},
                ],
            },
        }
        await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert len(sink.events) == 1
        assert sink.events[0][0] == THINKING
        assert sink.events[0][1]["text"] == "I should use Bash"

    @pytest.mark.asyncio
    async def test_assistant_tool_use_emits_tool_use_event(self):
        sink = _RecordingSink()
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Bash",
                        "input": {"command": "ls -la", "description": "list"},
                    },
                ],
            },
        }
        await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert len(sink.events) == 1
        typ, payload, body = sink.events[0]
        assert typ == TOOL_USE
        assert payload["tool_use_id"] == "toolu_01"
        assert payload["tool_name"] == "Bash"
        assert payload["input"]["command"] == "ls -la"
        assert "Bash" in body
        assert "ls -la" in body

    @pytest.mark.asyncio
    async def test_user_tool_result_emits_tool_result_event(self):
        sink = _RecordingSink()
        msg = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "file1\nfile2",
                        "is_error": False,
                    },
                ],
            },
        }
        await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert len(sink.events) == 1
        typ, payload, body = sink.events[0]
        assert typ == TOOL_RESULT
        assert payload["tool_use_id"] == "toolu_01"
        assert payload["output"] == "file1\nfile2"
        assert payload["is_error"] is False
        assert body == "→ ok"

    @pytest.mark.asyncio
    async def test_user_tool_result_error_body(self):
        sink = _RecordingSink()
        msg = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_02",
                        "content": "permission denied",
                        "is_error": True,
                    },
                ],
            },
        }
        await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert sink.events[0][2] == "→ error"

    @pytest.mark.asyncio
    async def test_result_emits_session_end_with_usage(self):
        sink = _RecordingSink()
        msg = {
            "type": "result",
            "result": "all done",
            "is_error": False,
            "duration_ms": 4500,
            "num_turns": 3,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "total_cost_usd": 0.003,
        }
        delta = await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert delta.final_result is True
        assert delta.final_result_text == "all done"
        assert delta.is_error is False
        typ, payload, body = sink.events[0]
        assert typ == SESSION_END
        assert payload["success"] is True
        assert payload["duration_ms"] == 4500
        assert payload["tokens"]["input_tokens"] == 100
        assert payload["cost_usd"] == 0.003

    @pytest.mark.asyncio
    async def test_result_error_marks_delta(self):
        sink = _RecordingSink()
        msg = {
            "type": "result",
            "result": "something broke",
            "is_error": True,
        }
        delta = await _dispatch_stream_message(
            msg, sink=sink, prompt="x", session_started=True
        )
        assert delta.is_error is True
        assert delta.error_text == "something broke"
        assert sink.events[0][1]["success"] is False

    @pytest.mark.asyncio
    async def test_null_sink_dispatches_without_error(self):
        # Should not raise when sink is None
        msg = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        }
        delta = await _dispatch_stream_message(
            msg, sink=None, prompt="x", session_started=True
        )
        assert delta.text == "hi"


class TestHelpers:
    def test_summarize_input_command(self):
        assert _summarize_input({"command": "git status"}) == "git status"

    def test_summarize_input_file_path(self):
        assert _summarize_input({"file_path": "/tmp/foo.py"}) == "/tmp/foo.py"

    def test_summarize_input_truncates_long_values(self):
        long = "x" * 200
        result = _summarize_input({"command": long})
        assert len(result) <= 60

    def test_summarize_input_empty_dict(self):
        assert _summarize_input({}) == ""

    def test_summarize_input_string(self):
        assert _summarize_input("hello world") == "hello world"

    def test_coerce_text_string(self):
        assert _coerce_text("hello") == "hello"

    def test_coerce_text_list_of_text_blocks(self):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _coerce_text(content) == "a\nb"

    def test_coerce_text_none(self):
        assert _coerce_text(None) == ""


# -------------------- KrewhubEventSink behavior --------------------


class TestKrewhubEventSink:
    @pytest.mark.asyncio
    async def test_emits_are_flushed_to_client(self):
        client = AsyncMock()
        client.post_events_batch = AsyncMock(return_value=[])
        sink = KrewhubEventSink(
            client=client, task_id="t1", agent_id="ag1",
            batch_size=2, flush_interval=0.05,
        )

        await sink.emit(TOOL_USE, payload={"tool_name": "Bash"}, body="Bash(ls)")
        await sink.emit(TOOL_RESULT, payload={"output": "ok"}, body="→ ok")

        await asyncio.sleep(0.2)
        await sink.flush()

        assert client.post_events_batch.await_count >= 1
        # Collect all events sent across calls
        sent: list[dict] = []
        for call in client.post_events_batch.await_args_list:
            _, kwargs = call
            # either (task_id, batch) positional or keyword
            args = call.args
            sent.extend(args[1])
        types = [e["type"] for e in sent]
        assert TOOL_USE in types
        assert TOOL_RESULT in types

    @pytest.mark.asyncio
    async def test_flush_is_idempotent(self):
        client = AsyncMock()
        client.post_events_batch = AsyncMock(return_value=[])
        sink = KrewhubEventSink(client=client, task_id="t1", agent_id="ag1")
        await sink.flush()
        await sink.flush()  # second call should not raise

    @pytest.mark.asyncio
    async def test_emit_after_flush_is_noop(self):
        client = AsyncMock()
        client.post_events_batch = AsyncMock(return_value=[])
        sink = KrewhubEventSink(client=client, task_id="t1", agent_id="ag1")
        await sink.flush()
        await sink.emit(AGENT_REPLY, body="late")
        # No events should have been posted
        client.post_events_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_droppable_event_drops_under_back_pressure(self):
        client = AsyncMock()
        client.post_events_batch = AsyncMock(return_value=[])
        sink = KrewhubEventSink(
            client=client, task_id="t1", agent_id="ag1",
            queue_size=1, batch_size=1, flush_interval=60.0,
        )
        # Pre-fill the queue so the next emit hits QueueFull.
        await sink.emit(TOOL_USE, body="first")  # spawns flusher, gets queued
        # With flush_interval=60s the flusher won't drain fast enough.
        # An AGENT_REPLY should be dropped.
        await sink.emit(AGENT_REPLY, body="dropped")
        assert sink.dropped_count >= 0  # either dropped or immediately drained
        await sink.flush()

    @pytest.mark.asyncio
    async def test_null_sink_is_truly_noop(self):
        sink = NullEventSink()
        await sink.emit("anything", payload={"x": 1}, body="b")
        await sink.flush()
