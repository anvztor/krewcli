from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflows.graph_renderer import render_graph
from krewcli.workflows.orchestrator_state import OrchestratorDeps, OrchestratorState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphExecutionResult:
    """Final result of a graph execution."""

    success: bool
    bundle_id: str
    mermaid: str = ""
    summary: str = ""
    task_results: dict = field(default_factory=dict)


class GraphExecutor:
    """Runs a pydantic-graph Graph with A2A dispatch at each step."""

    async def execute(
        self,
        graph,
        *,
        prompt: str,
        recipe_id: str,
        bundle_id: str,
        krewhub_client: KrewHubClient,
        a2a_client: httpx.AsyncClient,
        task_id_map: dict[str, str],
        agent_endpoints: dict[str, str],
        recipe_meta: dict[str, str],
        poll_interval: float = 3.0,
        task_timeout: float = 300.0,
    ) -> GraphExecutionResult:
        """Execute the graph, dispatching each step to an A2A agent."""
        state = OrchestratorState(
            prompt=prompt,
            recipe_id=recipe_id,
            bundle_id=bundle_id,
        )
        deps = OrchestratorDeps(
            krewhub_client=krewhub_client,
            a2a_client=a2a_client,
            task_id_map=task_id_map,
            agent_endpoints=agent_endpoints,
            recipe_meta=recipe_meta,
            poll_interval=poll_interval,
            task_timeout=task_timeout,
        )

        try:
            await graph.run(state=state, deps=deps)
            rendered = render_graph(graph, direction="LR")
            mermaid = rendered.mermaid

            all_success = all(r.success for r in state.task_results.values())

            return GraphExecutionResult(
                success=all_success,
                bundle_id=bundle_id,
                mermaid=mermaid,
                summary=f"Completed {len(state.task_results)} tasks"
                + (" (all passed)" if all_success else " (some failed)"),
                task_results={
                    k: {"success": v.success, "summary": v.summary}
                    for k, v in state.task_results.items()
                },
            )
        except Exception as exc:
            logger.exception("Graph execution failed")
            return GraphExecutionResult(
                success=False,
                bundle_id=bundle_id,
                summary=f"Graph execution failed: {exc}",
            )
