from __future__ import annotations

from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill

from krewcli.agents.registry import AGENT_REGISTRY


def build_agent_card(
    host: str,
    port: int,
    active_agents: list[str],
) -> AgentCard:
    """Build an A2A AgentCard from registered agents."""

    skills: list[AgentSkill] = []
    for name in active_agents:
        entry = AGENT_REGISTRY.get(name)
        if entry is None:
            continue
        skills.append(
            AgentSkill(
                id=name,
                name=entry["display_name"],
                description=f"Coding agent powered by {name}. "
                f"Capabilities: {', '.join(entry['capabilities'])}",
                tags=entry["capabilities"],
                examples=[
                    f"Use {name} to implement a heartbeat endpoint",
                    f"Use {name} to fix failing tests",
                ],
            )
        )

    base_url = f"http://{host}:{port}"

    return AgentCard(
        name="KrewCLI Agent Server",
        description="A2A agent server wrapping local coding agents (Codex, Claude, bub) "
        "for the Cookrew collaboration platform.",
        url=base_url,
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(transport="JSONRPC", url=base_url),
        ],
        skills=skills,
    )
