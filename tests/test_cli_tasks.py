"""Focused task-worker tests for ``krewcli.cli.tasks``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import krewcli.cli.tasks as tasks_mod


@pytest.mark.asyncio
async def test_run_task_worker_once_returns_false_when_heartbeat_is_busy():
    client = SimpleNamespace(list_tasks=None)
    runner = SimpleNamespace(claim_and_execute=None)
    heartbeat = SimpleNamespace(current_task_id="task_123")

    worked = await tasks_mod._run_task_worker_once(
        client=client,
        runner=runner,
        heartbeat=heartbeat,
        recipe_id="rec_1",
        agent_id="agent_1",
        digest_builders={},
    )

    assert worked is False


@pytest.mark.asyncio
async def test_run_task_worker_once_returns_false_when_no_open_tasks():
    class _Client:
        async def list_tasks(self, recipe_id: str):
            return [{"id": "task_1", "status": "done"}]

    runner = SimpleNamespace(claim_and_execute=None)
    heartbeat = SimpleNamespace(current_task_id=None)

    worked = await tasks_mod._run_task_worker_once(
        client=_Client(),
        runner=runner,
        heartbeat=heartbeat,
        recipe_id="rec_1",
        agent_id="agent_1",
        digest_builders={},
    )

    assert worked is False


@pytest.mark.asyncio
async def test_run_task_worker_once_returns_true_when_claim_fails():
    class _Client:
        async def list_tasks(self, recipe_id: str):
            return [{"id": "task_1", "bundle_id": "bun_1", "status": "open"}]

    class _Runner:
        async def claim_and_execute(self, task_id: str):
            return None

    worked = await tasks_mod._run_task_worker_once(
        client=_Client(),
        runner=_Runner(),
        heartbeat=SimpleNamespace(current_task_id=None),
        recipe_id="rec_1",
        agent_id="agent_1",
        digest_builders={},
    )

    assert worked is True


@pytest.mark.asyncio
async def test_run_task_worker_retries_after_cycle_error(monkeypatch):
    captured: dict[str, object] = {"worker_calls": 0}

    async def fake_load_recipe_context(client, recipe_id):
        captured["recipe_context"] = (client, recipe_id)
        return "git@example.com:repo.git", "develop"

    class _Runner:
        def __init__(self, **kwargs) -> None:
            captured["runner_kwargs"] = kwargs

    async def fake_worker_once(*args, **kwargs):
        captured["worker_calls"] += 1
        if captured["worker_calls"] == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError()

    async def fake_sleep(interval: float):
        captured.setdefault("sleep_intervals", []).append(interval)

    monkeypatch.setattr("krewcli.cli._load_recipe_context", fake_load_recipe_context, raising=False)
    monkeypatch.setattr("krewcli.cli.TaskRunner", _Runner, raising=False)
    monkeypatch.setattr(tasks_mod, "_run_task_worker_once", fake_worker_once)
    monkeypatch.setattr(tasks_mod.asyncio, "sleep", fake_sleep)

    settings = SimpleNamespace(task_poll_interval=0.25)
    heartbeat = SimpleNamespace(current_task_id=None)

    with pytest.raises(asyncio.CancelledError):
        await tasks_mod._run_task_worker(
            settings=settings,
            client="client-obj",
            heartbeat=heartbeat,
            recipe_id="rec_42",
            agent_name="claude",
            agent_id="agent_42",
            working_dir="/tmp/work",
        )

    assert captured["recipe_context"] == ("client-obj", "rec_42")
    assert captured["runner_kwargs"]["repo_url"] == "git@example.com:repo.git"
    assert captured["runner_kwargs"]["branch"] == "develop"
    assert captured["runner_kwargs"]["working_dir"] == "/tmp/work"
    assert captured["sleep_intervals"] == [0.25]
    assert captured["worker_calls"] == 2
