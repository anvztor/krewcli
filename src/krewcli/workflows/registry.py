"""Workflow registry: pydantic-graph templates that define task structures.

Each workflow is a Graph whose nodes become tasks and edges become
dependencies. The graph IS the plan — no LLM needed for decomposition.

graph_renderer.render_dependencies() renders the dependency flowchart.
Node names + edges become TaskSpecs sent to krewhub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pydantic_graph import Graph

from krewcli.workflows.graph_renderer import render_dependencies
from krewcli.workflows.templates import (
    feature_graph,
    bugfix_graph,
    refactor_graph,
    review_graph,
    default_graph,
)


@dataclass(frozen=True)
class TaskSpec:
    """A task extracted from a graph node."""
    id: str
    title: str
    description: str
    depends_on: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowSpec:
    """Output of workflow selection: tasks + dependency graph."""
    tasks: list[TaskSpec]
    mermaid: str
    workflow_name: str
    node_count: int = 0
    edge_count: int = 0


# Map intent keywords to workflow graphs
_WORKFLOWS: dict[str, tuple[str, Graph]] = {
    "feature": ("feature", feature_graph()),
    "bugfix": ("bugfix", bugfix_graph()),
    "refactor": ("refactor", refactor_graph()),
    "review": ("review", review_graph()),
    "default": ("default", default_graph()),
}


def list_workflows() -> list[str]:
    return list(_WORKFLOWS.keys())


def get_workflow(prompt: str) -> WorkflowSpec:
    """Select a workflow based on prompt intent and return task specs + dependency graph."""
    name, graph = _select_graph(prompt)
    tasks = extract_tasks(graph, prompt)
    rendered = render_dependencies(graph, tasks)
    return WorkflowSpec(
        tasks=tasks,
        mermaid=rendered.mermaid,
        workflow_name=name,
        node_count=rendered.node_count,
        edge_count=rendered.edge_count,
    )


def _select_graph(prompt: str) -> tuple[str, Graph]:
    lower = prompt.lower()

    if any(kw in lower for kw in ["refactor", "restructure", "migrate", "rewrite"]):
        return _WORKFLOWS["refactor"]
    if any(kw in lower for kw in ["fix", "debug", "resolve", "patch", "bug"]):
        return _WORKFLOWS["bugfix"]
    if any(kw in lower for kw in ["review", "audit", "check", "analyze"]):
        return _WORKFLOWS["review"]
    if any(kw in lower for kw in ["add", "implement", "create", "build", "ship", "develop"]):
        return _WORKFLOWS["feature"]

    return _WORKFLOWS["default"]


def extract_tasks(graph: Graph, prompt: str) -> list[TaskSpec]:
    """Extract TaskSpecs from graph.node_defs.

    node_defs keys are node names (strings), values have:
    - .node: the class (for docstring)
    - .next_node_edges: dict of outgoing edges (for reverse deps)
    """
    import re

    node_names = list(graph.node_defs.keys())
    node_ids = {name: f"task_{i}" for i, name in enumerate(node_names)}

    # Build reverse edges: which nodes lead INTO each node
    incoming: dict[str, list[str]] = {name: [] for name in node_names}
    for name, node_def in graph.node_defs.items():
        for target in node_def.next_node_edges:
            if target in incoming:
                incoming[target].append(name)

    excerpt = prompt.replace("\n", " ").strip()[:60]
    tasks: list[TaskSpec] = []

    for name, node_def in graph.node_defs.items():
        doc = (node_def.node.__doc__ or "").strip()
        readable = re.sub(r"(?<!^)(?=[A-Z])", " ", name)
        title = f"{readable}: {excerpt}"
        deps = [node_ids[dep] for dep in incoming[name] if dep in node_ids]

        tasks.append(TaskSpec(
            id=node_ids[name],
            title=title,
            description=doc,
            depends_on=deps,
        ))

    return tasks
