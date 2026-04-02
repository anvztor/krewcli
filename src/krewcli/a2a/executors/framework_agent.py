"""Tier 2 Agent: pydantic-ai framework with tools. (Stub — Phase C)"""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill


class FrameworkExecutor(AgentExecutor):
    def __init__(self, model: str, working_dir: str) -> None:
        self._model = model
        self._working_dir = working_dir

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("FrameworkExecutor not yet implemented (Phase C)")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def build_framework_card(provider: str, host: str, port: int) -> AgentCard:
    return AgentCard(
        name=f"framework:{provider}",
        description=f"pydantic-ai agent with coding tools via {provider}.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")],
        skills=[AgentSkill(id=f"code:{provider}", name=f"Framework Agent ({provider})", description="Stateful coding agent with tools.", tags=["code", "implement", "fix", "test"])],
    )
