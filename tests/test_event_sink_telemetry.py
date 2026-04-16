"""Tests for event sink drop telemetry."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from krewcli.agents.event_sink import KrewhubEventSink, AGENT_REPLY


class _FakeClient:
    """Minimal fake matching the KrewHubClient.post_events_batch shape."""
    def __init__(self):
        self.batches: list[list[dict]] = []
        self.post_events_batch = AsyncMock(side_effect=self._record)

    async def _record(self, task_id: str, events: list[dict]):
        self.batches.append(events)
        return events


@pytest.mark.asyncio
async def test_no_telemetry_when_nothing_dropped():
    client = _FakeClient()
    sink = KrewhubEventSink(client, task_id="task_x", agent_id="agent_x")

    await sink.emit("tool_use", payload={"tool": "bash"}, body="bash(ls)")
    await sink.flush()

    # Flatten all events sent
    all_events = [e for batch in client.batches for e in batch]
    telemetry_events = [e for e in all_events if (e.get("payload") or {}).get("_telemetry") == "event_sink"]
    assert len(telemetry_events) == 0


@pytest.mark.asyncio
async def test_telemetry_emitted_when_events_dropped():
    client = _FakeClient()
    # Use tiny queue to force drops
    sink = KrewhubEventSink(
        client, task_id="task_x", agent_id="agent_x",
        queue_size=2, batch_size=1, flush_interval=10.0,
    )

    # Fill queue beyond capacity with droppable events
    # Queue has size 2. The 3rd+ AGENT_REPLY will be dropped.
    for i in range(20):
        await sink.emit(AGENT_REPLY, payload={"text": f"r{i}"}, body=f"r{i}")

    assert sink.dropped_count > 0

    await sink.flush()

    all_events = [e for batch in client.batches for e in batch]
    telemetry_events = [e for e in all_events if (e.get("payload") or {}).get("_telemetry") == "event_sink"]
    assert len(telemetry_events) == 1
    telemetry = telemetry_events[0]
    assert telemetry["payload"]["dropped_count"] > 0
    assert "dropped" in telemetry["body"].lower()


@pytest.mark.asyncio
async def test_telemetry_contains_config():
    client = _FakeClient()
    sink = KrewhubEventSink(
        client, task_id="t", agent_id="a",
        queue_size=1, batch_size=1, flush_interval=10.0,
    )
    # Force drops
    for i in range(10):
        await sink.emit(AGENT_REPLY, body=f"msg{i}")
    await sink.flush()

    all_events = [e for batch in client.batches for e in batch]
    telemetry = next(e for e in all_events if (e.get("payload") or {}).get("_telemetry") == "event_sink")
    assert telemetry["payload"]["batch_size"] == 1
    assert telemetry["payload"]["queue_size"] == 1
    assert telemetry["payload"]["dropped_count"] >= 8
