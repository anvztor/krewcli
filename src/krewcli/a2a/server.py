from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard


def create_a2a_app(
    agent_card: AgentCard,
    executor: AgentExecutor,
) -> A2AStarletteApplication:
    """Create an A2A Starlette application for any executor type."""

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    return A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
