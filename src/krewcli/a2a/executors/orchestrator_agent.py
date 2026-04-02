"""Tier 3 Agent: Orchestrator with pydantic-graph. (Stub — Phase D)"""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill


class OrchestratorExecutor(AgentExecutor):
    def __init__(self, model: str, krewhub_url: str, api_key: str) -> None:
        self._model = model
        self._krewhub_url = krewhub_url
        self._api_key = api_key

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("OrchestratorExecutor not yet implemented (Phase D)")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def build_orchestrator_card(host: str, port: int) -> AgentCard:
    return AgentCard(
        name="orchestrator",
        description="Decomposes complex prompts into sub-tasks, dispatches to other agents, monitors and synthesizes results.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")],
        skills=[AgentSkill(id="orchestrate", name="Orchestrator", description="Decompose, dispatch, and synthesize multi-step work.", tags=["orchestrate", "decompose", "coordinate"])],
    )
