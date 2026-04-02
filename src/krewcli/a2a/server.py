from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard

from krewcli.a2a.plan_endpoint import plan_routes


def create_a2a_app(
    agent_card: AgentCard,
    executor: AgentExecutor,
    plan_model: str = "anthropic:claude-sonnet-4-20250514",
) -> A2AStarletteApplication:
    """Create an A2A Starlette application with a /plan REST endpoint."""

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    # Mount /plan REST endpoint for LLM-based task decomposition
    starlette_app = app.build()
    starlette_app.state.plan_model = plan_model
    for route in plan_routes:
        starlette_app.routes.append(route)

    return app
