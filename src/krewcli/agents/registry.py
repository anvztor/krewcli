from __future__ import annotations

from typing import Any

from krewcli.agents.base import AgentRunner
from krewcli.agents.codex_agent import create_codex_agent
from krewcli.agents.claude_agent import create_claude_agent
from krewcli.agents.bub_agent import create_bub_agent

# Every worker also advertises "generate-graph" so krewhub's
# PlannerDispatchController can route empty-bundle planning requests to
# any onboarded agent. The gateway executor detects planning requests
# (metadata has bundle_id but no task_id) and runs the CLI with the
# codegen prompt template, POSTing the result to /bundles/{id}/graph.
AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "codex": {
        "factory": create_codex_agent,
        "display_name": "Codex Agent",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
    "claude": {
        "factory": create_claude_agent,
        "display_name": "Claude Agent",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
    "bub": {
        "factory": create_bub_agent,
        "display_name": "Bub Agent",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
}


def get_agent(name: str) -> AgentRunner:
    entry = AGENT_REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"Unknown agent: {name}. Available: {list(AGENT_REGISTRY.keys())}")
    return entry["factory"]()


def get_agent_info(name: str) -> dict[str, Any]:
    entry = AGENT_REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"Unknown agent: {name}")
    return {
        "display_name": entry["display_name"],
        "capabilities": entry["capabilities"],
    }
