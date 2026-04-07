"""End-to-end test: orchestrator flow via registered A2A agent.

Flow:
  1. Start live krewhub
  2. Start a fake A2A agent (HTTP) that handles both code-gen and task execution
  3. Register the fake agent in krewhub
  4. Create cookbook → recipe
  5. Invoke OrchestratorExecutor with a bundle prompt
  6. Orchestrator asks the fake agent for GraphBuilder code
  7. Orchestrator creates bundle with tasks
  8. Orchestrator dispatches each graph step to the fake agent
  9. Fake agent marks tasks done via krewhub callback
 10. Verify: bundle status == cooked
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from krewcli.client.krewhub_client import KrewHubClient

KREWHUB_PROJECT_PATH = Path(__file__).resolve().parents[2] / "krewhub"
KREWHUB_BIN_PATH = KREWHUB_PROJECT_PATH / ".venv" / "bin" / "krewhub"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"local socket bind not permitted in this environment: {exc}")
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
    raise AssertionError(f"Server at {base_url} did not become ready")


# ---------------------------------------------------------------------------
# Fake A2A Agent — responds to both code-gen and task-dispatch requests
# ---------------------------------------------------------------------------

# Graph code the fake agent returns when asked to generate a workflow.
# Simple 2-step: scope → implement.  Enough to prove the pipeline.
FAKE_GRAPH_CODE = """\
g = GraphBuilder(
    state_type=OrchestratorState,
    deps_type=OrchestratorDeps,
    output_type=str,
)

@g.step
async def scope(ctx: StepContext[OrchestratorState, OrchestratorDeps, None]) -> str:
    return await dispatch_and_wait(ctx, "scope")

@g.step
async def implement(ctx: StepContext[OrchestratorState, OrchestratorDeps, str]) -> str:
    return await dispatch_and_wait(ctx, "implement")

g.add(
    g.edge_from(g.start_node).to(scope),
    g.edge_from(scope).to(implement),
    g.edge_from(implement).to(g.end_node),
)

graph = g.build()
"""


def _build_fake_agent_app(krewhub_base_url: str, api_key: str) -> Starlette:
    """Build a Starlette app that mimics an A2A agent (gateway pattern).

    Handles two kinds of dispatched tasks:
    1. Codegen tasks (prompt contains "GraphBuilder API"): reports back via
       krewhub callback with full_output = FAKE_GRAPH_CODE
    2. Normal tasks: reports back via krewhub callback marking done

    Uses the krewhub A2A callback endpoint, just like the real gateway does.
    """

    async def handle_jsonrpc(request: Request) -> JSONResponse:
        body = await request.json()
        params = body.get("params", {})
        message = params.get("message", {})
        metadata = message.get("metadata", {})
        request_id = body.get("id", "")
        task_id = metadata.get("task_id", "")

        # Extract prompt text
        prompt_text = ""
        for part in message.get("parts", []):
            if part.get("kind") == "text":
                prompt_text = part.get("text", "")
                break

        is_codegen = "GraphBuilder API" in prompt_text

        if task_id:
            # Report result via krewhub callback (mimics real gateway)
            callback_payload = {
                "task_id": task_id,
                "agent_id": "fake_claude_agent",
                "success": True,
                "summary": "Generated graph code" if is_codegen else "Task completed",
                "full_output": FAKE_GRAPH_CODE if is_codegen else "",
            }
            async with httpx.AsyncClient(
                base_url=krewhub_base_url,
                headers={"X-API-Key": api_key},
                timeout=10.0,
            ) as hub:
                # First claim the task (move from open → claimed)
                await hub.post(
                    f"/api/v1/tasks/{task_id}/claim",
                    json={"agent_id": "fake_claude_agent"},
                )
                # Then report completion via callback
                await hub.post(
                    "/api/v1/a2a/callback",
                    json=callback_payload,
                )

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "id": f"a2a_task_{uuid.uuid4().hex[:8]}",
                "status": {"state": "working"},
            },
        })

    return Starlette(routes=[Route("/", handle_jsonrpc, methods=["POST"])])


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_e2e_bundle_cooked(tmp_path):
    """Full e2e: register agent → prompt → orchestrate → dispatch → cooked."""

    krewhub_port = _get_free_port()
    agent_port = _get_free_port()
    api_key = "e2e-orchestrator-key"
    krewhub_url = f"http://127.0.0.1:{krewhub_port}"
    agent_url = f"http://127.0.0.1:{agent_port}"
    db_path = tmp_path / "krewhub-orch-e2e.sqlite3"

    # 1. Start krewhub
    krewhub_proc = await asyncio.create_subprocess_exec(
        *_krewhub_command(),
        cwd=str(KREWHUB_PROJECT_PATH),
        env={
            **os.environ,
            "KREWHUB_HOST": "127.0.0.1",
            "KREWHUB_PORT": str(krewhub_port),
            "KREWHUB_DATABASE_PATH": str(db_path),
            "KREWHUB_API_KEY": api_key,
        },
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    # 2. Start fake A2A agent
    import uvicorn

    fake_app = _build_fake_agent_app(krewhub_url, api_key)
    agent_config = uvicorn.Config(
        fake_app, host="127.0.0.1", port=agent_port, log_level="warning",
    )
    agent_server = uvicorn.Server(agent_config)
    agent_task = asyncio.create_task(agent_server.serve())

    try:
        await _wait_for_server(krewhub_url)
        # Wait briefly for the agent server to bind
        await asyncio.sleep(0.5)

        hub_client = KrewHubClient(krewhub_url, api_key)

        try:
            # 3. Create cookbook + recipe
            cookbook = await hub_client.create_cookbook(
                name="orch-e2e-cookbook", owner_id="e2e_user",
            )
            cookbook_id = cookbook["id"]

            recipe_resp = await hub_client.create_recipe(
                name="test/orchestrator-e2e",
                repo_url="git@github.com:test/orch-e2e.git",
                created_by="e2e_user",
                cookbook_id=cookbook_id,
            )
            recipe_id = recipe_resp["id"]

            # 4. Register the fake agent in krewhub
            await hub_client.register_agent(
                agent_id="fake_claude_agent",
                cookbook_id=cookbook_id,
                display_name="Fake Claude",
                capabilities=["code", "implement", "test"],
                max_concurrent_tasks=5,
                endpoint_url=agent_url,
            )

            # Verify agent is registered
            agents = await hub_client.list_agents(cookbook_id)
            assert len(agents) >= 1
            assert any(a["agent_id"] == "fake_claude_agent" for a in agents)

            # 5. Run OrchestratorExecutor
            from a2a.server.agent_execution import RequestContext
            from a2a.types import (
                Message,
                MessageSendParams,
                Part,
                TextPart,
            )
            from krewcli.a2a.executors.orchestrator_agent import OrchestratorExecutor

            executor = OrchestratorExecutor(
                krewhub_client=hub_client,
                cookbook_id=cookbook_id,
            )

            # Build real RequestContext + mock EventQueue
            text_part = TextPart(text="Add user authentication with login and signup")
            message = Message(
                message_id=uuid.uuid4().hex,
                role="user",
                parts=[Part(root=text_part)],
                metadata={
                    "recipe_id": recipe_id,
                    "recipe_name": "test/orchestrator-e2e",
                    "repo_url": "git@github.com:test/orch-e2e.git",
                    "branch": "main",
                },
            )
            params = MessageSendParams(message=message)

            task_id_a2a = f"a2a_{uuid.uuid4().hex[:8]}"
            context_id_a2a = f"ctx_{uuid.uuid4().hex[:8]}"
            context = RequestContext(
                request=params,
                task_id=task_id_a2a,
                context_id=context_id_a2a,
            )

            collected_events = []
            mock_queue = AsyncMock()
            mock_queue.enqueue_event = AsyncMock(
                side_effect=lambda evt: collected_events.append(evt)
            )

            # 6. Execute the orchestrator
            await executor.execute(context, mock_queue)

            # 7. Verify results
            # Find the artifact event with the result JSON
            result_json = None
            for evt in collected_events:
                if hasattr(evt, "artifact"):
                    for part in evt.artifact.parts:
                        if hasattr(part, "root") and hasattr(part.root, "text"):
                            result_json = json.loads(part.root.text)
                        elif hasattr(part, "text"):
                            result_json = json.loads(part.text)

            assert result_json is not None, (
                f"No result artifact found in events: "
                f"{[type(e).__name__ for e in collected_events]}"
            )
            assert result_json["workflow"] == "agent_generated"
            assert result_json["success"] is True
            assert result_json["bundle_id"] != ""

            bundle_id = result_json["bundle_id"]

            # 8. Verify bundle is cooked in krewhub
            bundle_detail = await hub_client.get_bundle(bundle_id)
            assert bundle_detail["bundle"]["status"] == "cooked"

            # Verify all tasks are done
            for task in bundle_detail["tasks"]:
                assert task["status"] == "done", (
                    f"Task {task['id']} has status {task['status']}, expected done"
                )

            # Verify mermaid diagram was generated
            assert result_json["mermaid"] != ""

            # Verify task_results has entries for both graph steps
            assert len(result_json["task_results"]) == 2

        finally:
            await hub_client.close()

    finally:
        # Shutdown fake agent
        agent_server.should_exit = True
        await asyncio.sleep(0.1)
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass

        # Shutdown krewhub
        if krewhub_proc.returncode is None:
            krewhub_proc.terminate()
            try:
                await asyncio.wait_for(krewhub_proc.wait(), timeout=5.0)
            except TimeoutError:
                krewhub_proc.kill()
                await krewhub_proc.wait()
