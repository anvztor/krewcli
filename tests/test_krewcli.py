from __future__ import annotations

import pytest
from click.testing import CliRunner
import httpx

from krewcli.agents.models import TaskResult, FactRefResult, CodeRefResult
from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.cli import main, _load_recipe_context, _run_task_worker_once
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflow.digest_builder import DigestBuilder


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


def test_claim_command_reports_blocked_task(monkeypatch):
    class FakeHeartbeatLoop:
        def __init__(self, *args, **kwargs) -> None:
            self.current_task_id = None

        def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class FakeTaskRunner:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def claim_and_execute(self, task_id: str) -> TaskResult:
            assert task_id == "task_1"
            return TaskResult(
                summary="Could not complete",
                success=False,
                blocked_reason="Missing dependency",
            )

    monkeypatch.setattr("krewcli.cli.HeartbeatLoop", FakeHeartbeatLoop)
    monkeypatch.setattr("krewcli.cli.TaskRunner", FakeTaskRunner)
    async def fake_load_recipe_context(client, recipe_id: str):
        assert recipe_id == "rec_1"
        return "git@github.com:test/repo.git", "main"

    monkeypatch.setattr("krewcli.cli._load_recipe_context", fake_load_recipe_context)

    runner = CliRunner()
    result = runner.invoke(main, ["claim", "task_1", "--recipe", "rec_1"])

    assert result.exit_code == 0
    assert "Task task_1 blocked: Missing dependency" in result.output
    assert "completed" not in result.output


def test_digest_builder_add_and_clear():
    client = KrewHubClient("http://fake:1234", "key")
    builder = DigestBuilder(client=client, agent_id="test_agent")

    result_a = TaskResult(
        summary="Task A done",
        facts=[FactRefResult(claim="Fact 1")],
    )
    result_b = TaskResult(
        summary="Task B done",
        code_refs=[
            CodeRefResult(
                repo_url="git@github.com:org/repo.git",
                branch="main",
                commit_sha="def456",
                paths=["src/b.py"],
            )
        ],
    )

    builder.add_result("task_1", result_a)
    builder.add_result("task_2", result_b)
    assert len(builder._results) == 2

    builder.clear()
    assert len(builder._results) == 0


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
        assert request.method == "POST"
        payload = request.read().decode()
        assert '"decision":"approved"' in payload
        assert '"decided_by":"qa.lead"' in payload
        return httpx.Response(
            200,
            json={
                "digest": {
                    "id": "dig_1",
                    "bundle_id": "bun_1",
                    "decision": "approved",
                }
            },
        )

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8420",
        headers={"X-API-Key": "test-key"},
    )

    try:
        digest = await client.post_decision("bun_1", "approved", "qa.lead", "Ship it")
    finally:
        await client.close()

    assert digest == {
        "id": "dig_1",
        "bundle_id": "bun_1",
        "decision": "approved",
    }


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


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.current_task_id: str | None = None


class _FakeRunner:
    async def claim_and_execute(self, task_id: str) -> TaskResult:
        assert task_id == "task_1"
        return TaskResult(summary="Finished task", success=True)


class _FakeClient:
    def __init__(self) -> None:
        self.digest_submissions: list[str] = []

    async def list_tasks(self, recipe_id: str):
        assert recipe_id == "rec_1"
        return [{"id": "task_1", "bundle_id": "bun_1", "status": "open"}]

    async def get_bundle(self, bundle_id: str):
        assert bundle_id == "bun_1"
        return {
            "bundle": {"id": "bun_1", "status": "cooked"},
            "tasks": [{"id": "task_1"}],
            "events": [],
        }

    async def submit_digest(self, bundle_id: str, submitted_by: str, summary: str, task_results, facts, code_refs):
        self.digest_submissions.append(bundle_id)
        return {"id": "dig_1", "bundle_id": bundle_id, "summary": summary, "submitted_by": submitted_by}


@pytest.mark.asyncio
async def test_run_task_worker_once_claims_and_submits_digest():
    client = _FakeClient()
    digest_builders: dict[str, DigestBuilder] = {}

    worked = await _run_task_worker_once(
        client=client,
        runner=_FakeRunner(),
        heartbeat=_FakeHeartbeat(),
        recipe_id="rec_1",
        agent_id="agent_1",
        digest_builders=digest_builders,
    )

    assert worked is True
    assert client.digest_submissions == ["bun_1"]
    assert digest_builders == {}
