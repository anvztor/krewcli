"""Render pydantic-graph workflow dependencies as mermaid flowcharts.

graph.mermaid_code() produces stateDiagram-v2 (state transitions).
This module renders dependency relationships as flowchart TD — showing
which tasks block which, with readable labels and status styling.

Supports both the stable Graph API (node_defs) and the beta
GraphBuilder API (nodes dict with edges).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from krewcli.workflows.registry import TaskSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderedGraph:
    """Output of dependency rendering."""

    mermaid: str
    node_count: int
    edge_count: int


def render_dependencies_from_tasks(tasks: Sequence[TaskSpec]) -> RenderedGraph:
    """Render task dependencies as a mermaid flowchart from TaskSpecs.

    Produces a flowchart TD where:
    - Each node is a task with its readable title
    - Edges represent depends_on relationships (dependency -> dependent)
    - Root tasks (no dependencies) get a distinct style
    - Leaf tasks (no dependents) get a distinct style
    """
    lines = ["flowchart TD"]
    edge_count = 0

    valid_ids = {t.id for t in tasks}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}

    for task in tasks:
        for dep_id in task.depends_on:
            if dep_id in dependents:
                dependents[dep_id].append(task.id)

    for task in tasks:
        label = _readable_label(task.title)
        escaped = _escape_mermaid(label)
        lines.append(f'    {_safe_id(task.id)}["{escaped}"]')

    for task in tasks:
        for dep_id in task.depends_on:
            if dep_id in valid_ids:
                lines.append(f"    {_safe_id(dep_id)} --> {_safe_id(task.id)}")
                edge_count += 1

    roots = [t.id for t in tasks if not t.depends_on]
    leaves = [t.id for t in tasks if not dependents[t.id]]

    if roots:
        lines.append("    classDef root fill:#e8f5e9,stroke:#4caf50,stroke-width:2px")
        lines.append(f"    class {','.join(_safe_id(r) for r in roots)} root")

    if leaves:
        lines.append("    classDef leaf fill:#e3f2fd,stroke:#2196f3,stroke-width:2px")
        lines.append(f"    class {','.join(_safe_id(l) for l in leaves)} leaf")

    return RenderedGraph(
        mermaid="\n".join(lines),
        node_count=len(tasks),
        edge_count=edge_count,
    )


# Keep backward-compatible alias
def render_dependencies(graph: Any, tasks: Sequence[TaskSpec]) -> RenderedGraph:
    """Render task dependencies as a mermaid flowchart.

    Prefer the graph's own node/edge structure so the rendered dependency
    diagram stays aligned with the workflow template. When possible, graph
    nodes are mapped onto the existing TaskSpec IDs and readable labels so
    downstream consumers keep the same task-oriented mermaid identifiers.

    Falls back to TaskSpec dependency information if the graph cannot be
    introspected or its node count does not match the extracted tasks.
    """
    node_ids, edges = _extract_graph_structure(graph)
    if not node_ids:
        return render_dependencies_from_tasks(tasks)

    if len(node_ids) != len(tasks):
        logger.debug(
            "Graph/task mismatch when rendering dependencies: %s graph nodes vs %s tasks",
            len(node_ids),
            len(tasks),
        )
        return render_dependencies_from_tasks(tasks)

    aliases = {node_id: task.id for node_id, task in zip(node_ids, tasks, strict=True)}
    labels = {node_id: _readable_label(task.title) for node_id, task in zip(node_ids, tasks, strict=True)}
    return _render_mermaid(
        node_ids,
        edges,
        direction="TD",
        id_aliases=aliases,
        label_overrides=labels,
    )


_VALID_DIRECTIONS = frozenset({"TD", "TB", "BT", "LR", "RL"})


def render_graph(graph: Any, *, direction: str = "TD") -> RenderedGraph:
    """Render a pydantic-graph Graph's dependency structure as a mermaid flowchart.

    Works with both stable Graph (node_defs) and beta GraphBuilder graphs (nodes dict).
    This replaces the nonexistent graph.render() with a proper dependency renderer.
    """
    node_ids, edges = _extract_graph_structure(graph)
    return _render_mermaid(node_ids, edges, direction=direction)


def render_dependencies_from_graph(graph: Any, prompt: str) -> RenderedGraph:
    """Convenience: extract tasks from graph and render dependencies in one call."""
    from krewcli.workflows.registry import extract_tasks

    tasks = extract_tasks(graph, prompt)
    return render_dependencies_from_tasks(tasks)


def _extract_graph_structure(graph: Any) -> tuple[list[str], list[tuple[str, str]]]:
    """Extract node IDs and edges from either stable or beta graph API.

    Returns (node_ids, edges) where edges are (source, target) tuples.
    """
    node_ids: list[str] = []
    edges: list[tuple[str, str]] = []

    # Beta GraphBuilder API: graph.nodes dict + edge metadata
    if hasattr(graph, "nodes") and isinstance(getattr(graph, "nodes", None), dict):
        skip = {"__start__", "__end__"}
        skip_types = {"StartNode", "EndNode", "ForkNode", "JoinNode"}

        for name, node in graph.nodes.items():
            if name in skip:
                continue
            if type(node).__name__ in skip_types:
                continue
            node_ids.append(name)

        node_set = set(node_ids)

        # Extract edges from graph._edges or similar
        if hasattr(graph, "_edges"):
            for edge in graph._edges:
                src = getattr(edge, "source", None) or getattr(edge, "from_node", None)
                dst = getattr(edge, "target", None) or getattr(edge, "to_node", None)
                if src and dst:
                    src_name = src if isinstance(src, str) else getattr(src, "name", str(src))
                    dst_name = dst if isinstance(dst, str) else getattr(dst, "name", str(dst))
                    if src_name in node_set and dst_name in node_set:
                        edges.append((src_name, dst_name))

        # Fallback: use mermaid_code to parse edges if available
        if not edges and hasattr(graph, "mermaid_code"):
            try:
                mermaid = graph.mermaid_code()
                edges = _parse_mermaid_edges(mermaid, node_set)
            except Exception:
                logger.debug("Failed to parse mermaid_code for edges", exc_info=True)

        if node_ids:
            return node_ids, edges

    # Stable Graph API: graph.node_defs
    if hasattr(graph, "node_defs"):
        for name in graph.node_defs:
            if not name.startswith("__"):
                node_ids.append(name)

        node_set = set(node_ids)
        for name, node_def in graph.node_defs.items():
            if name.startswith("__"):
                continue
            if hasattr(node_def, "next_node_edges"):
                for target in node_def.next_node_edges:
                    if target in node_set:
                        edges.append((name, target))

        return node_ids, edges

    logger.warning("Cannot extract structure from graph type %s", type(graph))
    return node_ids, edges


def _render_mermaid(
    node_ids: list[str],
    edges: list[tuple[str, str]],
    *,
    direction: str,
    id_aliases: dict[str, str] | None = None,
    label_overrides: dict[str, str] | None = None,
) -> RenderedGraph:
    """Render mermaid flowchart text from a normalized node/edge list."""
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction '{direction}', must be one of {sorted(_VALID_DIRECTIONS)}")

    if not node_ids:
        return RenderedGraph(mermaid=f"flowchart {direction}", node_count=0, edge_count=0)

    aliases = id_aliases or {}
    labels = label_overrides or {}
    mermaid_ids = {nid: _safe_id(aliases.get(nid, nid)) for nid in node_ids}

    lines = [f"flowchart {direction}"]
    edge_count = 0

    incoming: dict[str, list[str]] = {nid: [] for nid in node_ids}
    outgoing: dict[str, list[str]] = {nid: [] for nid in node_ids}

    for src, dst in edges:
        if src in incoming and dst in incoming:
            incoming[dst].append(src)
            outgoing[src].append(dst)

    for nid in node_ids:
        label = _escape_mermaid(labels.get(nid, _humanize(nid)))
        lines.append(f'    {mermaid_ids[nid]}["{label}"]')

    for src, dst in edges:
        if src in incoming and dst in incoming:
            lines.append(f"    {mermaid_ids[src]} --> {mermaid_ids[dst]}")
            edge_count += 1

    roots = [nid for nid in node_ids if not incoming[nid]]
    leaves = [nid for nid in node_ids if not outgoing[nid]]

    if roots:
        lines.append("    classDef root fill:#e8f5e9,stroke:#4caf50,stroke-width:2px")
        lines.append(f"    class {','.join(mermaid_ids[r] for r in roots)} root")

    if leaves:
        lines.append("    classDef leaf fill:#e3f2fd,stroke:#2196f3,stroke-width:2px")
        lines.append(f"    class {','.join(mermaid_ids[l] for l in leaves)} leaf")

    return RenderedGraph(
        mermaid="\n".join(lines),
        node_count=len(node_ids),
        edge_count=edge_count,
    )


def _parse_mermaid_edges(mermaid: str, valid_nodes: set[str]) -> list[tuple[str, str]]:
    """Parse edges from mermaid code as a fallback for beta graphs."""
    edges: list[tuple[str, str]] = []
    arrow_pattern = re.compile(r"(\S+)\s*-->\s*(\S+)")

    for line in mermaid.splitlines():
        match = arrow_pattern.search(line.strip())
        if match:
            src, dst = match.group(1), match.group(2)
            if src in valid_nodes and dst in valid_nodes:
                edges.append((src, dst))

    return edges


def _readable_label(title: str) -> str:
    """Extract the readable node name from a task title like 'Feature Scope: ...'."""
    parts = title.split(":", 1)
    return parts[0].strip() if parts else title


def _humanize(node_id: str) -> str:
    """Convert a node ID like 'scope' or 'FeatureScope' to readable text."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", node_id)
    return spaced.replace("_", " ").title()


def _safe_id(node_id: str) -> str:
    """Ensure a node ID is valid for mermaid (alphanumeric + underscore)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", node_id)


def _escape_mermaid(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
