"""Multi-agent A2A gateway server.

Creates a single Starlette application that mounts a separate A2A app
for each detected agent type at /agents/{name}. Each agent gets its
own AgentCard, executor, and capacity tracking.

    /agents/claude/.well-known/agent.json   → Claude agent card
    /agents/claude                          → A2A JSON-RPC
    /agents/codex/.well-known/agent.json    → Codex agent card
    /agents/codex                           → A2A JSON-RPC
    /health                                 → gateway health

Every worker advertises the `generate-graph` capability so krewhub's
PlannerDispatchController can route empty-bundle planning requests to
any of them. When a worker receives a planning request (identified by
``metadata.bundle_id`` without ``metadata.task_id``), its GatewayExecutor
runs the CLI with the codegen prompt template, captures the output, and
POSTs the resulting graph code to /api/v1/bundles/{id}/graph directly.
No separate planner process, no standalone agent — every worker is a
planner by default.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from krewcli.a2a.executors.gateway import GatewayExecutor, build_gateway_agent_card
from krewcli.a2a.spawn_manager import SpawnManager
from krewcli.agents.registry import AGENT_REGISTRY

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient


def create_gateway_app(
    host: str,
    port: int,
    working_dir: str,
    repo_url: str = "",
    branch: str = "main",
    callback_url: str = "",
    api_key: str = "",
    agent_names: list[str] | None = None,
    max_concurrent: int = 1,
    recipe_contexts: dict[str, dict] | None = None,
    krewhub_url: str = "",
    workspace_dir: str = "",
    *,
    krewhub_client: "KrewHubClient | None" = None,
    cookbook_id: str = "",
) -> tuple[Starlette, SpawnManager, list[str]]:
    """Create a multi-agent gateway Starlette app.

    Auto-detects available CLIs on PATH if agent_names is None. Each
    worker's GatewayExecutor receives the shared ``krewhub_client`` so
    it can POST graph code to /bundles/{id}/graph when a planning
    request arrives (see GatewayExecutor.execute for the routing).

    Args:
        krewhub_client: Shared KrewHubClient used by planning requests
            to attach graph code back to the originating bundle. Can be
            None in test contexts where no planning is exercised.
        cookbook_id: Default cookbook id for agent discovery during
            planning — passed through to GatewayExecutor.

    Returns:
        (app, spawn_manager, registered_agent_names).
    """
    # Detect available agents
    if agent_names is None:
        agent_names = [
            name for name in AGENT_REGISTRY
            if shutil.which(name) is not None
        ]

    if not agent_names:
        agent_names = list(AGENT_REGISTRY.keys())[:1]

    spawn_manager = SpawnManager(
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        callback_url=callback_url,
        api_key=api_key,
        recipe_contexts=recipe_contexts,
        krewhub_url=krewhub_url,
        workspace_dir=workspace_dir or working_dir,
    )

    mounts: list[Mount | Route] = []

    for name in agent_names:
        card = build_gateway_agent_card(name, host, port)
        agent_id = f"{name}@{host}:{port}"

        executor = GatewayExecutor(
            agent_name=name,
            spawn_manager=spawn_manager,
            agent_id=agent_id,
            max_concurrent=max_concurrent,
            krewhub_client=krewhub_client,
            cookbook_id=cookbook_id,
        )

        handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=InMemoryTaskStore(),
        )

        a2a_app = A2AStarletteApplication(
            agent_card=card,
            http_handler=handler,
        )

        sub_app = a2a_app.build()
        mounts.append(Mount(f"/agents/{name}", app=sub_app))

    async def _health(request: Request) -> JSONResponse:
        agents_status = {}
        for name in agent_names:
            agents_status[name] = {
                "available": shutil.which(name) is not None,
                "running": spawn_manager.running_count_for(name),
            }
        return JSONResponse({
            "status": "ok",
            "total_running": spawn_manager.running_count,
            "agents": agents_status,
        })

    mounts.append(Route("/health", _health))

    app = Starlette(routes=mounts)

    return app, spawn_manager, list(agent_names)
