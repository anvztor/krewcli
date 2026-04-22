from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
import httpx

from krewcli.agents.models import TaskResult, FactRefResult, CodeRefResult
from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.cli import main, _load_recipe_context
from krewcli.client.krewhub_client import KrewHubClient


def test_agent_registry_has_all_agents():
    assert "codex" in AGENT_REGISTRY
    assert "claude" in AGENT_REGISTRY
    assert "bub" in AGENT_REGISTRY


def test_get_agent_info():
    info = get_agent_info("codex")
    assert info["display_name"] == "Codex Agent"
    assert "claim" in info["capabilities"]


def test_get_agent_info_unknown():
    with pytest.raises(ValueError, match="Unknown agent"):
        get_agent_info("nonexistent")


def test_task_result_model():
    result = TaskResult(
        summary="Added heartbeat endpoint",
        files_modified=["server/heartbeat.py"],
        facts=[
            FactRefResult(claim="Heartbeat < 30s = online", confidence=0.95),
        ],
        code_refs=[
            CodeRefResult(
                repo_url="git@github.com:org/repo.git",
                branch="feat/heartbeat",
                commit_sha="abc123",
                paths=["server/heartbeat.py"],
            ),
        ],
        success=True,
    )
    assert result.success
    assert len(result.facts) == 1
    assert result.facts[0].claim == "Heartbeat < 30s = online"


def test_task_result_blocked():
    result = TaskResult(
        summary="Could not complete",
        success=False,
        blocked_reason="Missing dependency on auth module",
    )
    assert not result.success
    assert result.blocked_reason is not None


def test_cli_status_command():
    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "codex" in result.output
    assert "claude" in result.output
    assert "bub" in result.output


def test_krewhub_client_instantiation():
    client = KrewHubClient("http://127.0.0.1:8420", "test-key")
    assert client._client.base_url == "http://127.0.0.1:8420"
    assert client._client.headers["x-api-key"] == "test-key"


@pytest.mark.asyncio
async def test_krewhub_client_list_tasks_aggregates_bundle_data():
    client = KrewHubClient("http://127.0.0.1:8420", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recipes/rec_1/bundles":
            return httpx.Response(
                200,
                json={
                    "bundles": [
                        {"id": "bun_1", "status": "open", "prompt": "First bundle"},
                        {"id": "bun_2", "status": "digested", "prompt": "Done"},
                    ]
                },
            )
        if request.url.path == "/api/v1/bundles/bun_1":
            return httpx.Response(
                200,
                json={
                    "bundle": {"id": "bun_1", "status": "open"},
                    "tasks": [{"id": "task_1", "bundle_id": "bun_1", "status": "open", "title": "A"}],
                    "events": [],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8420",
        headers={"X-API-Key": "test-key"},
    )

    try:
        tasks = await client.list_tasks("rec_1")
    finally:
        await client.close()

    assert tasks == [
        {
            "id": "task_1",
            "bundle_id": "bun_1",
            "status": "open",
            "title": "A",
            "bundle_status": "open",
            "bundle_prompt": "First bundle",
        }
    ]


@pytest.mark.asyncio
async def test_krewhub_client_post_decision():
    client = KrewHubClient("http://127.0.0.1:8420", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bundles/bun_1/decision"
        return httpx.Response(200, json={"digest": {"id": "dig_1", "bundle_id": "bun_1", "decision": "approved"}})

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8420",
        headers={"X-API-Key": "test-key"},
    )

    try:
        digest = await client.post_decision("bun_1", "approved", "qa.lead", "Ship it")
    finally:
        await client.close()

    assert digest["decision"] == "approved"


@pytest.mark.asyncio
async def test_krewhub_client_post_event_uses_empty_payload_by_default():
    client = KrewHubClient("http://127.0.0.1:8420", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content.decode())["payload"] == {}
        return httpx.Response(200, json={"event": {"id": "evt_1"}})

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8420",
        headers={"X-API-Key": "test-key"},
    )

    try:
        event = await client.post_event(task_id="task_1", event_type="milestone", actor_id="agent_1", body="Done")
    finally:
        await client.close()

    assert event == {"id": "evt_1"}


@pytest.mark.asyncio
async def test_load_recipe_context_uses_recipe_metadata():
    class _RecipeClient:
        async def get_recipe(self, recipe_id: str):
            assert recipe_id == "rec_1"
            return {
                "recipe": {
                    "repo_url": "git@github.com:test/repo.git",
                    "default_branch": "release/test",
                }
            }

    repo_url, branch = await _load_recipe_context(_RecipeClient(), "rec_1")

    assert repo_url == "git@github.com:test/repo.git"
    assert branch == "release/test"
