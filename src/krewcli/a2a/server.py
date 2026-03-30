from __future__ import annotations

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from krewcli.a2a.card import build_agent_card
from krewcli.a2a.executor import KrewAgentExecutor


def create_a2a_app(
    host: str,
    port: int,
    default_agent: str,
    active_agents: list[str],
    working_dir: str,
    repo_url: str = "",
    branch: str = "main",
) -> A2AStarletteApplication:
    """Create the A2A Starlette application."""

    agent_card = build_agent_card(host, port, active_agents)

    executor = KrewAgentExecutor(
        default_agent_name=default_agent,
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
    )

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    return A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
