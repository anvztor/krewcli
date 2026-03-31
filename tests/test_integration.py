from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from time import monotonic

import httpx
import pytest

from krewcli.agents.base import AgentDeps, AgentRunResult
from krewcli.agents.models import CodeRefResult, FactRefResult, TaskResult
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.workflow.digest_builder import DigestBuilder
from krewcli.workflow.task_runner import TaskRunner

KREWHUB_PROJECT_PATH = Path(__file__).resolve().parents[2] / "krewhub"
KREWHUB_BIN_PATH = KREWHUB_PROJECT_PATH / ".venv" / "bin" / "krewhub"


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _krewhub_command() -> list[str]:
    if KREWHUB_BIN_PATH.exists():
        return [str(KREWHUB_BIN_PATH)]
    return ["uv", "run", "--project", str(KREWHUB_PROJECT_PATH), "krewhub"]


async def _wait_for_server(base_url: str, timeout_seconds: float = 20.0) -> None:
    deadline = monotonic() + timeout_seconds
    async with httpx.AsyncClient(base_url=base_url, timeout=1.0) as client:
        while monotonic() < deadline:
            try:
                response = await client.get("/openapi.json")
            except httpx.HTTPError:
                response = None

            if response is not None and response.status_code == 200:
                return

            await asyncio.sleep(0.1)

    raise AssertionError(f"KrewHub server at {base_url} did not become ready in time")


class _FakeAgent:
    async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult:
        assert "Stitch the krewcli integration flow together." in prompt
        assert deps.repo_url == "git@github.com:test/phase4-krewcli.git"
        assert deps.branch == "feat/integration"
        return AgentRunResult(
            output=TaskResult(
                summary="Completed the integration task and captured evidence.",
                facts=[
                    FactRefResult(
                        claim="KrewCLI can send milestone evidence into KrewHub."
                    )
                ],
                code_refs=[
                    CodeRefResult(
                        repo_url=deps.repo_url,
                        branch=deps.branch,
                        commit_sha="abc1234",
                        paths=["src/workflow/task_runner.py"],
                    )
                ],
                success=True,
            )
        )


@pytest.mark.asyncio
async def test_krewcli_can_run_full_flow_against_live_krewhub(
    tmp_path,
    monkeypatch,
):
    port = _get_free_port()
    api_key = "integration-test-key"
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "krewhub-integration.sqlite3"

    process = await asyncio.create_subprocess_exec(
        *_krewhub_command(),
        cwd=str(KREWHUB_PROJECT_PATH),
        env={
            **os.environ,
            "KREWHUB_HOST": "127.0.0.1",
            "KREWHUB_PORT": str(port),
            "KREWHUB_DATABASE_PATH": str(db_path),
            "KREWHUB_API_KEY": api_key,
        },
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await _wait_for_server(base_url)

        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=10.0,
        ) as api_client:
            recipe_response = await api_client.post(
                "/api/v1/recipes",
                json={
                    "name": "test/krewcli-phase4",
                    "repo_url": "git@github.com:test/phase4-krewcli.git",
                    "default_branch": "feat/integration",
                    "created_by": "qa.lead",
                },
            )
            assert recipe_response.status_code == 200
            recipe_id = recipe_response.json()["recipe"]["id"]

            bundle_response = await api_client.post(
                f"/api/v1/recipes/{recipe_id}/bundles",
                json={
                    "prompt": "Stitch the krewcli integration flow together.",
                    "requested_by": "qa.lead",
                    "tasks": [{"title": "Stitch the krewcli integration flow together."}],
                },
            )
            assert bundle_response.status_code == 200
            bundle_id = bundle_response.json()["bundle"]["id"]
            task_id = bundle_response.json()["tasks"][0]["id"]

            hub_client = KrewHubClient(base_url, api_key)
            heartbeat = HeartbeatLoop(
                client=hub_client,
                agent_id="codex_phase4",
                recipe_id=recipe_id,
                display_name="Codex Phase 4",
                capabilities=["claim", "milestones", "digests"],
                interval=1,
            )

            try:
                heartbeat.start()
                await asyncio.sleep(0.2)

                recipe_detail = await hub_client.get_recipe(recipe_id)
                assert recipe_detail["agents"][0]["agent_id"] == "codex_phase4"

                monkeypatch.setattr(
                    "krewcli.workflow.task_runner.get_agent",
                    lambda _name: _FakeAgent(),
                )

                runner = TaskRunner(
                    client=hub_client,
                    heartbeat=heartbeat,
                    agent_name="codex",
                    agent_id="codex_phase4",
                    working_dir=str(tmp_path),
                    repo_url=recipe_detail["recipe"]["repo_url"],
                    branch=recipe_detail["recipe"]["default_branch"],
                )

                result = await runner.claim_and_execute(task_id)
                assert result is not None
                assert result.success is True

                bundle_detail = await hub_client.get_bundle(bundle_id)
                assert bundle_detail["bundle"]["status"] == "cooked"
                assert bundle_detail["tasks"][0]["status"] == "done"
                assert any(
                    event["type"] == "milestone"
                    and event["body"] == "Completed the integration task and captured evidence."
                    for event in bundle_detail["events"]
                )

                digest_builder = DigestBuilder(client=hub_client, agent_id="codex_phase4")
                digest_builder.add_result(task_id, result)
                digest = await digest_builder.submit(bundle_id)
                assert digest is not None
                assert digest["decision"] == "pending"

                approved_digest = await hub_client.post_decision(
                    bundle_id=bundle_id,
                    decision="approved",
                    decided_by="qa.lead",
                    note="Approve the live KrewCLI integration flow.",
                )
                assert approved_digest["decision"] == "approved"

                history_response = await api_client.get(f"/api/v1/recipes/{recipe_id}/digests")
                assert history_response.status_code == 200
                assert history_response.json()["digests"][0]["bundle_id"] == bundle_id

            finally:
                await heartbeat.stop()
                await hub_client.close()

    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
