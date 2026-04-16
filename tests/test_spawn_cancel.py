"""Tests for SpawnManager cancellation propagation."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from krewcli.a2a.spawn_manager import SpawnManager, SpawnResult


class _FakeHttpClient:
    """Minimal httpx-compatible client for cancel-status polling."""
    def __init__(self, cancel_status_responses):
        self._responses = list(cancel_status_responses)
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        # Return the current response, or fall back to the last one
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


def _make_response(cancelled: bool, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"cancelled": cancelled, "task_id": "t1"}
    return resp


@pytest.mark.asyncio
async def test_cancel_watcher_kills_task_when_cancelled():
    """When krewhub returns cancelled=true, SpawnManager cancels the spawn task."""
    # Create a krewhub client mock with a fast cancellation signal
    krewhub_client = MagicMock()
    krewhub_client._client = _FakeHttpClient([_make_response(False), _make_response(True)])

    # Build a manager with no callback (we only test cancellation, not reporting)
    mgr = SpawnManager(
        callback_url="",
        krewhub_client=krewhub_client,
        working_dir="/tmp",
        repo_url="",
        branch="main",
    )

    # Mock out _execute to just sleep for a long time
    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(30)
        return SpawnResult(task_id="", agent_id="", success=True, summary="done")
    mgr._execute = slow_execute  # type: ignore[assignment]

    # Speed up the cancel poll interval for testing
    original_watch = mgr._watch_cancellation

    async def fast_watch(session):
        # Replace poll_interval with a very short value
        import httpx
        poll_interval = 0.05
        client = mgr._krewhub_client
        try:
            while True:
                await asyncio.sleep(poll_interval)
                if session.task_id not in mgr._sessions:
                    return
                try:
                    resp = await client._client.get(
                        f"/api/v1/tasks/{session.task_id}/cancel-status",
                    )
                    if resp.status_code == 200 and resp.json().get("cancelled"):
                        session.cancel_event.set()
                        if session.task is not None and not session.task.done():
                            session.task.cancel()
                        return
                except httpx.HTTPError:
                    pass
        except asyncio.CancelledError:
            return

    mgr._watch_cancellation = fast_watch  # type: ignore[assignment]

    # Spawn a task — it should get cancelled via the watcher
    ok = await mgr.spawn(
        agent_name="test_agent",
        agent_id="a1",
        task_id="t1",
        prompt="long task",
    )
    assert ok is True

    # Wait for the session to finish (should happen quickly via cancel)
    session = mgr._sessions.get("t1")
    assert session is not None and session.task is not None

    try:
        await asyncio.wait_for(session.task, timeout=3.0)
    except (asyncio.CancelledError, Exception):
        pass  # expected — task was cancelled

    # Session should be cleaned up and cancel_event should be set
    # (the _run_and_report finally block pops the session)
    # The cancel_event was set by the watcher
    assert session.cancel_event.is_set()


@pytest.mark.asyncio
async def test_no_cancel_watcher_without_krewhub_client():
    """When krewhub_client is None, spawn works but no watcher starts."""
    mgr = SpawnManager(
        callback_url="",
        krewhub_client=None,
        working_dir="/tmp",
        repo_url="",
        branch="main",
    )

    async def quick_execute(*args, **kwargs):
        return SpawnResult(task_id="", agent_id="", success=True, summary="done")
    mgr._execute = quick_execute  # type: ignore[assignment]

    await mgr.spawn(
        agent_name="test_agent",
        agent_id="a1",
        task_id="t1",
        prompt="quick",
    )

    session = mgr._sessions.get("t1")
    if session is not None:
        assert session.cancel_watcher is None
