from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from krewcli.client.krewhub_client import KrewHubClient


@dataclass(frozen=True)
class TaskNodeResult:
    """Result of a single graph step's A2A execution."""

    node_id: str
    task_id: str
    success: bool
    summary: str = ""


@dataclass
class OrchestratorState:
    """Mutable state flowing through the orchestrator graph."""

    prompt: str
    recipe_id: str = ""
    bundle_id: str = ""
    task_results: dict[str, TaskNodeResult] = field(default_factory=dict)


@dataclass
class OrchestratorDeps:
    """Dependencies injected into every graph step."""

    krewhub_client: KrewHubClient
    a2a_client: httpx.AsyncClient
    task_id_map: dict[str, str] = field(default_factory=dict)
    agent_endpoints: dict[str, str] = field(default_factory=dict)
    recipe_meta: dict[str, str] = field(default_factory=dict)
    poll_interval: float = 3.0
    task_timeout: float = 300.0
