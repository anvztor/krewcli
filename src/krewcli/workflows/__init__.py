"""Workflow helpers used by the planner agent.

Trimmed to the two modules the new krewhub-driven flow actually needs:

    - llm_planner.generate_graph_code: dispatches a codegen task to a worker
      agent (claude/codex/etc.) and returns the resulting pydantic-graph
      source code as a string. Used by PlannerOrchestratorExecutor as the
      default code generator.
    - agent_dispatch: A2A POST helper + krewhub task polling helper used
      internally by llm_planner to round-trip the codegen task.

Everything else (graph_builder/executor/graph_renderer/orchestrator_state/
registry/templates) lived here for the legacy monolithic OrchestratorExecutor
flow and was deleted when that executor was removed. Krewhub now owns the
sandbox, the renderer, the runtime state types, and the graph runner.
"""

from krewcli.workflows.llm_planner import generate_graph_code

__all__ = ["generate_graph_code"]
