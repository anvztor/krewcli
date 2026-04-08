"""End-to-end test for the krewhub-driven planner + runner flow.

Exercises the full loop introduced across the seven graph-runtime sessions:

    1. Real krewhub subprocess starts (with PlannerDispatchController and
       GraphRunnerController in its ControllerManager).
    2. Two fake A2A endpoints come up — one playing the planner role
       (POSTs hardcoded graph code to /bundles/{id}/graph), one playing
       the worker role (POSTs task completions to /a2a/callback).
    3. Both fakes are registered in krewhub with appropriate capabilities.
    4. An empty bundle is created (status=open, graph_code=null, no tasks).
    5. PlannerDispatchController spots the empty bundle, dispatches the
       planner. The planner POSTs back hardcoded graph code. krewhub's
       BundleService.attach_graph_artifact validates + creates tasks.
    6. GraphRunnerController picks up the now-runnable bundle and runs
       graph.iter() against it. dispatch_cycle dispatches each step to
       the worker, which posts completion via callback.
    7. Once all tasks are done, the runner marks the bundle COOKED.
    8. The test polls the bundle row until it reaches COOKED, with a
       timeout. No executor is invoked manually.

This is the new replacement for test_orchestrator_e2e.py — that test
drives the legacy monolithic OrchestratorExecutor and is left in place
(skipped) for reference.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from pathlib import Path
from time import monotonic

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
# Hardcoded graph the fake planner returns
# ---------------------------------------------------------------------------

# Two-step linear graph using the new dispatch_cycle helper. Both steps
# share the same task_kind so a single worker capability covers them.
FAKE_GRAPH_CODE = '''
g = GraphBuilder(state_type=OrchestratorState, deps_type=OrchestratorDeps, output_type=str)

@g.step
async def scope(ctx: StepContext[OrchestratorState, OrchestratorDeps, None]) -> str:
    return await dispatch_cycle(
        ctx, node_id="scope", task_kind="coder",
        instruction="Plan the work", max_iterations=2,
    )

@g.step
async def implement(ctx: StepContext[OrchestratorState, OrchestratorDeps, str]) -> str:
    return await dispatch_cycle(
        ctx, node_id="implement", task_kind="coder",
        instruction="Implement it", max_iterations=2,
    )

g.add(
    g.edge_from(g.start_node).to(scope),
    g.edge_from(scope).to(implement),
    g.edge_from(implement).to(g.end_node),
)
graph = g.build()
'''


# ---------------------------------------------------------------------------
# Process / network helpers
# ---------------------------------------------------------------------------


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"local socket bind not permitted: {exc}")
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
                resp = await client.get("/openapi.json")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise AssertionError(f"server at {base_url} did not become ready")


async def _wait_for_bundle_status(
    hub: KrewHubClient,
    bundle_id: str,
    target_status: str,
    *,
    timeout: float = 30.0,
) -> dict:
    """Poll the bundle row until status matches target. Raise on timeout."""
    deadline = monotonic() + timeout
    last: dict = {}
    while monotonic() < deadline:
        last = await hub.get_bundle(bundle_id)
        if last["bundle"]["status"] == target_status:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"bundle {bundle_id} did not reach status={target_status!r} within "
        f"{timeout}s; last={last['bundle'].get('status')!r}, "
        f"reason={last['bundle'].get('blocked_reason')!r}"
    )


# ---------------------------------------------------------------------------
# Fake planner agent
# ---------------------------------------------------------------------------


def _build_fake_planner_app(krewhub_base_url: str, api_key: str) -> Starlette:
    """A2A planner stub: when invoked, POSTs hardcoded graph code back to krewhub.

    Reads bundle_id from message metadata. Responds 200 to the inbound A2A
    request immediately, then attaches the graph code asynchronously.
    """

    async def handle_jsonrpc(request: Request) -> JSONResponse:
        body = await request.json()
        params = body.get("params", {})
        message = params.get("message", {})
        metadata = message.get("metadata", {})
        request_id = body.get("id", "")
        bundle_id = metadata.get("bundle_id", "")

        if bundle_id:
            # Attach the graph asynchronously so we don't block the A2A response.
            async def attach():
                async with httpx.AsyncClient(
                    base_url=krewhub_base_url,
                    headers={"X-API-Key": api_key},
                    timeout=10.0,
                ) as hub:
                    await hub.post(
                        f"/api/v1/bundles/{bundle_id}/graph",
                        json={"code": FAKE_GRAPH_CODE, "created_by": "fake-planner"},
                    )

            asyncio.create_task(attach())

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "id": f"a2a_{uuid.uuid4().hex[:8]}",
                "status": {"state": "submitted"},
            },
        })

    return Starlette(routes=[Route("/", handle_jsonrpc, methods=["POST"])])


# ---------------------------------------------------------------------------
# Fake worker agent
# ---------------------------------------------------------------------------


def _build_fake_worker_app(krewhub_base_url: str, api_key: str) -> Starlette:
    """A2A worker stub: marks dispatched tasks done via /a2a/callback."""

    async def handle_jsonrpc(request: Request) -> JSONResponse:
        body = await request.json()
        params = body.get("params", {})
        message = params.get("message", {})
        metadata = message.get("metadata", {})
        request_id = body.get("id", "")
        task_id = metadata.get("task_id", "")

        if task_id:
            # Complete the task asynchronously so we return 200 quickly.
            async def complete():
                async with httpx.AsyncClient(
                    base_url=krewhub_base_url,
                    headers={"X-API-Key": api_key},
                    timeout=10.0,
                ) as hub:
                    # Mark task working first so the /a2a/callback's
                    # CLAIMED-or-WORKING precondition holds.
                    await hub.post(
                        "/api/v1/a2a/callback",
                        json={
                            "task_id": task_id,
                            "agent_id": "fake-worker",
                            "success": True,
                            "summary": "completed by fake worker",
                        },
                    )

            asyncio.create_task(complete())

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "id": f"a2a_{uuid.uuid4().hex[:8]}",
                "status": {"state": "submitted"},
            },
        })

    return Starlette(routes=[Route("/", handle_jsonrpc, methods=["POST"])])


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_flow_e2e_empty_bundle_to_cooked(tmp_path):
    """Empty bundle → planner dispatch → graph runner → COOKED, no manual exec."""
    krewhub_port = _get_free_port()
    planner_port = _get_free_port()
    worker_port = _get_free_port()
    api_key = "e2e-planner-key"
    krewhub_url = f"http://127.0.0.1:{krewhub_port}"
    planner_url = f"http://127.0.0.1:{planner_port}"
    worker_url = f"http://127.0.0.1:{worker_port}"
    db_path = tmp_path / "krewhub-planner-e2e.sqlite3"

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

    # 2. Start fake planner + worker A2A apps
    import uvicorn

    planner_app = _build_fake_planner_app(krewhub_url, api_key)
    planner_config = uvicorn.Config(
        planner_app, host="127.0.0.1", port=planner_port, log_level="warning",
    )
    planner_server = uvicorn.Server(planner_config)
    planner_task = asyncio.create_task(planner_server.serve())

    worker_app = _build_fake_worker_app(krewhub_url, api_key)
    worker_config = uvicorn.Config(
        worker_app, host="127.0.0.1", port=worker_port, log_level="warning",
    )
    worker_server = uvicorn.Server(worker_config)
    worker_task = asyncio.create_task(worker_server.serve())

    try:
        await _wait_for_server(krewhub_url)
        await asyncio.sleep(0.5)  # let the agent servers bind

        hub_client = KrewHubClient(krewhub_url, api_key)

        try:
            # 3. Cookbook + recipe
            cookbook = await hub_client.create_cookbook(
                name="planner-e2e-cookbook", owner_id="e2e_user",
            )
            cookbook_id = cookbook["id"]

            recipe = await hub_client.create_recipe(
                name="test/planner-e2e",
                repo_url="git@github.com:test/planner-e2e.git",
                created_by="e2e_user",
                cookbook_id=cookbook_id,
            )
            recipe_id = recipe["id"]

            # 4. Register both fake agents in krewhub.
            #    Planner advertises 'generate-graph' so PlannerDispatchController
            #    picks it up. Worker advertises 'coder' to match the graph's
            #    task_kind, so dispatch_cycle picks it for each step.
            await hub_client.register_agent(
                agent_id="fake-planner",
                cookbook_id=cookbook_id,
                display_name="Fake Planner",
                capabilities=["generate-graph"],
                max_concurrent_tasks=4,
                endpoint_url=planner_url,
            )
            await hub_client.register_agent(
                agent_id="fake-worker",
                cookbook_id=cookbook_id,
                display_name="Fake Worker",
                capabilities=["coder"],
                max_concurrent_tasks=4,
                endpoint_url=worker_url,
            )

            # 5. Create an EMPTY bundle (no manual tasks).
            #    PlannerDispatchController will pick it up on its next reconcile.
            bundle, tasks = await hub_client.create_bundle(
                recipe_id=recipe_id,
                prompt="Add user authentication with login and signup",
                requested_by="e2e_user",
                tasks=[],
            )
            bundle_id = bundle["id"]
            assert tasks == []
            assert bundle["status"] == "open"

            # 6. Wait for the bundle to reach COOKED.
            #    This exercises: PlannerDispatchController → fake-planner POST →
            #    BundleService.attach_graph_artifact → GraphRunnerController →
            #    dispatch_cycle → fake-worker → /a2a/callback → bundle COOKED.
            cooked = await _wait_for_bundle_status(
                hub_client, bundle_id, "cooked", timeout=30.0,
            )

            # 7. Verify the bundle has graph_code and graph_mermaid attached.
            assert cooked["bundle"]["graph_code"] is not None
            assert "GraphBuilder" in cooked["bundle"]["graph_code"]
            assert cooked["bundle"]["graph_mermaid"] is not None
            assert "flowchart" in cooked["bundle"]["graph_mermaid"]

            # 8. Verify both graph nodes became tasks and all are done.
            assert len(cooked["tasks"]) == 2
            node_ids = {t["graph_node_id"] for t in cooked["tasks"]}
            assert node_ids == {"scope", "implement"}
            for task in cooked["tasks"]:
                assert task["status"] == "done", (
                    f"task {task['id']} ({task.get('graph_node_id')}) "
                    f"is {task['status']}"
                )

            # 9. Verify the implement task depends on scope (edge preserved).
            by_node = {t["graph_node_id"]: t for t in cooked["tasks"]}
            scope_task = by_node["scope"]
            implement_task = by_node["implement"]
            assert scope_task["depends_on_task_ids"] == []
            assert implement_task["depends_on_task_ids"] == [scope_task["id"]]

        finally:
            await hub_client.close()

    finally:
        # Shutdown fake servers
        planner_server.should_exit = True
        worker_server.should_exit = True
        await asyncio.sleep(0.1)
        planner_task.cancel()
        worker_task.cancel()
        for t in (planner_task, worker_task):
            try:
                await t
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
