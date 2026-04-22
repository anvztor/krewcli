"""Unit tests for the daemon module (session, harness, recovery, loop)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from krewcli.backend.protocol import BackendMessage, BackendResult, BackendSession
from krewcli.daemon.session import Session
from krewcli.daemon.recovery import recover_orphans


class _FakeClient:
    """Minimal mock for KrewHubClient."""

    def __init__(self):
        self.posted_batches: list[list[dict]] = []
        self.completion_calls: list[tuple] = []
        self.usage_calls: list[dict] = []
        self.cancel_status = False

    async def post_events_batch(self, task_id, events):
        self.posted_batches.append(events)
        return [{"id": f"evt_{i}"} for i in range(len(events))]

    async def post_task_completion(self, task_id, session_id, work_dir, artifacts=None):
        self.completion_calls.append((task_id, session_id, work_dir))
        return {"id": task_id}

    async def post_task_usage(self, task_id, input_tokens, output_tokens, **kwargs):
        self.usage_calls.append({
            "task_id": task_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            **kwargs,
        })
        return {}

    async def poll_cancel_status(self, task_id):
        return self.cancel_status

    async def get_working_tasks(self, agent_ids):
        return []

    async def update_task_status(self, task_id, status, blocked_reason=None):
        return {"id": task_id, "status": status}


class TestSession:
    @pytest.mark.asyncio
    async def test_session_append_and_flush(self):
        client = _FakeClient()
        session = Session(client, "task_1", "agent_1", flush_interval=0.05)

        await session.append("session_start", body="start")
        await session.append("agent_reply", body="hello")
        await session.append("session_end", body="done")

        # Give the flush loop time to fire
        await asyncio.sleep(0.2)
        await session.flush()

        all_events = []
        for batch in client.posted_batches:
            all_events.extend(batch)

        types = [e["type"] for e in all_events]
        assert "session_start" in types
        assert "agent_reply" in types
        assert "session_end" in types

    @pytest.mark.asyncio
    async def test_session_pin(self):
        client = _FakeClient()
        session = Session(client, "task_1", "agent_1")

        await session.pin("session_abc", "/tmp/work")

        assert len(client.completion_calls) == 1
        assert client.completion_calls[0] == ("task_1", "session_abc", "/tmp/work")

    @pytest.mark.asyncio
    async def test_session_append_from_backend(self):
        client = _FakeClient()
        session = Session(client, "task_1", "agent_1", flush_interval=0.05)

        msg = BackendMessage(kind="tool_use", body="Read(file.py)", payload={"tool_name": "Read"})
        await session.append_from_backend(msg)
        await asyncio.sleep(0.2)
        await session.flush()

        all_events = []
        for batch in client.posted_batches:
            all_events.extend(batch)

        assert any(e["type"] == "tool_use" for e in all_events)

    @pytest.mark.asyncio
    async def test_session_check_cancelled(self):
        client = _FakeClient()
        session = Session(client, "task_1", "agent_1")

        assert await session.check_cancelled() is False
        client.cancel_status = True
        assert await session.check_cancelled() is True

    @pytest.mark.asyncio
    async def test_session_report_usage(self):
        client = _FakeClient()
        session = Session(client, "task_1", "agent_1")

        await session.report_usage({
            "input_tokens": 100,
            "output_tokens": 50,
            "model": "claude",
            "cost_usd": 0.01,
        })

        assert len(client.usage_calls) == 1
        assert client.usage_calls[0]["input_tokens"] == 100


class TestRecovery:
    @pytest.mark.asyncio
    async def test_recover_orphans_no_stuck_tasks(self):
        client = _FakeClient()
        count = await recover_orphans(client, ["agent_1@owner"])
        assert count == 0

    @pytest.mark.asyncio
    async def test_recover_orphans_marks_stuck_tasks(self):
        client = _FakeClient()
        client.get_working_tasks = AsyncMock(return_value=[
            {"id": "task_1", "status": "working", "claimed_by_agent_id": "agent_1@owner"},
        ])
        client.update_task_status = AsyncMock(return_value={"id": "task_1", "status": "blocked"})

        count = await recover_orphans(client, ["agent_1@owner"])

        assert count == 1
        client.update_task_status.assert_called_once_with(
            "task_1",
            status="blocked",
            blocked_reason="daemon_crash_recovery: task was in-flight when daemon stopped",
        )
