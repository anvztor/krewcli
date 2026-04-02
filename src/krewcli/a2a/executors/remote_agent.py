"""Tier 2 Agent: Remote A2A/ACP proxy. (Stub — Phase E)"""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill


class RemoteExecutor(AgentExecutor):
    def __init__(self, remote_url: str) -> None:
        self._remote_url = remote_url

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("RemoteExecutor not yet implemented (Phase E)")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def build_remote_card(remote_url: str, host: str, port: int) -> AgentCard:
    return AgentCard(
        name=f"remote:{remote_url}",
        description=f"Proxy to remote A2A agent at {remote_url}.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")],
        skills=[AgentSkill(id="remote", name="Remote Agent", description=f"Proxy to {remote_url}", tags=["code"])],
    )
