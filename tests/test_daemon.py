"""Unit tests for the daemon module (session, harness, recovery, loop)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from krewcli.backend.protocol import BackendMessage, BackendResult, BackendSession
from krewcli.daemon.harness import HarnessResult
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


class _PollClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.claims: list[tuple[str, str]] = []
        self.claimable = [
            {
                "id": "task_1",
                "bundle_id": "bun_1",
                "cookbook_id": "cb_1",
                "title": "ship from cookrew-beta",
                "description": "created through the UI",
                "status": "open",
                "depends_on_task_ids": [],
                "assigned_runtime_id": "rt_stale",
                "sandbox_id": "sbx_1",
            },
        ]

    async def poll_claimable_tasks(self, cookbook_id):
        assert cookbook_id == "cb_1"
        return self.claimable

    async def claim_task(self, task_id, agent_id):
        self.claims.append((task_id, agent_id))
        return {
            **self.claimable[0],
            "status": "claimed",
            "claimed_by_agent_id": agent_id,
        }


class TestDaemonLoopCookrewPolling:
    @pytest.mark.asyncio
    async def test_poll_claims_cookrew_task_and_runs_harness(self, tmp_path, monkeypatch):
        from krewcli.backend.echo import EchoBackend
        from krewcli.daemon.loop import DaemonLoop

        executed: list[dict] = []

        async def fake_execute(self, **kwargs):
            executed.append(kwargs)
            return HarnessResult(success=True, summary="ok")

        monkeypatch.setattr("krewcli.daemon.loop.Harness.execute", fake_execute)

        client = _PollClient()
        loop = DaemonLoop(
            client=client,
            backends={"echo": EchoBackend()},
            cookbook_id="cb_1",
            working_dir=str(tmp_path),
            max_concurrent=1,
        )
        loop._agent_ids = {"echo": "echo@krew"}

        await loop._poll_claimable_tasks()
        await asyncio.wait_for(asyncio.gather(*list(loop._task_jobs)), timeout=1)

        assert client.claims == [("task_1", "echo@krew")]
        assert len(executed) == 1
        assert executed[0]["task_id"] == "task_1"
        assert executed[0]["bundle_id"] == "bun_1"
        assert "ship from cookrew-beta" in executed[0]["prompt"]

    @pytest.mark.asyncio
    async def test_poll_respects_global_capacity(self, tmp_path, monkeypatch):
        from krewcli.backend.echo import EchoBackend
        from krewcli.daemon.loop import DaemonLoop

        client = _PollClient()
        loop = DaemonLoop(
            client=client,
            backends={"echo": EchoBackend()},
            cookbook_id="cb_1",
            working_dir=str(tmp_path),
            max_concurrent=1,
        )
        loop._running_tasks.add("busy_task")

        await loop._poll_claimable_tasks()

        assert client.claims == []
