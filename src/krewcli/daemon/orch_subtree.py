"""Subtree view + reconcile for the orch-agent (gap 5, C5).

The orch-agent (Row-0 brain) owns one task A and drives a *subtree* of
children it spawned via subagent links. This module builds the brain's
local view of that subtree from krewhub's public API (bundle tasks +
links) and detects drift between turns.

It is read-only: it never mutates krewhub. Spawning is the brain's job
(``spawn_subtask``); mechanical respawn/cascade is krewhub's
``OrchController``. The reconcile here is *level-triggered awareness* —
it tells the brain what changed so it can decide reroute/retire, and
flags drift the mechanical reconciler can't reason about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Task statuses that mean "this child is no longer making progress".
TERMINAL_STATUSES = frozenset({"done", "blocked", "cancelled"})


@dataclass(frozen=True)
class ChildState:
    """One node in A's subtree, as the brain sees it."""

    task_id: str
    title: str
    status: str
    kind: str  # "subagent" | "pipe"
    link_id: str | None
    parent_task_id: str
    depth: int  # 1 = direct child of root, 2 = grandchild, …

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class SubtreeView:
    """Immutable snapshot of A's orchestration subtree at one instant."""

    root_task_id: str
    bundle_id: str
    children: tuple[ChildState, ...]

    def direct_children(self) -> tuple[ChildState, ...]:
        return tuple(c for c in self.children if c.depth == 1)

    def pending(self) -> tuple[ChildState, ...]:
        """Children still making progress (not terminal)."""
        return tuple(c for c in self.children if not c.is_terminal)

    def terminal(self) -> tuple[ChildState, ...]:
        return tuple(c for c in self.children if c.is_terminal)

    def all_done(self) -> bool:
        """True when there is at least one child and every one is done."""
        kids = self.children
        return bool(kids) and all(c.is_done for c in kids)

    def terminal_signature(self) -> frozenset[tuple[str, str]]:
        """The set of (task_id, status) for terminal children.

        Drives the loop's turn trigger: when a child crosses into a
        terminal status the signature changes and the brain wakes. New
        ``open``/``working`` children (e.g. just-spawned) don't change it,
        so the brain isn't woken by its own spawns — only by progress.
        """
        return frozenset(
            (c.task_id, c.status) for c in self.children if c.is_terminal
        )


def _index_tasks(bundle_detail: dict) -> dict[str, dict]:
    tasks = bundle_detail.get("tasks", [])
    return {t["id"]: t for t in tasks if t.get("id")}


def build_subtree(
    root_task_id: str,
    bundle_id: str,
    tasks_by_id: dict[str, dict],
    links: list[dict],
) -> SubtreeView:
    """Walk drives links (out-edges) from the root to assemble the subtree.

    v3: a single "drives" link primitive — every active outgoing link
    "A drives B" makes B a child of A, regardless of how it was created
    (agent-spawned OR human runtime-adoption). Behavior is derived from
    link topology, not provenance: ``created_by_task`` is metadata, not a
    gate (an adopted child has ``created_by_task != A`` yet is still A's
    child). The walk is bounded + cycle-safe via the ``seen`` set.
    """
    # Active (non-revoked) links keyed by from_task_id.
    out_links: dict[str, list[dict]] = {}
    for lk in links:
        if lk.get("revoked_at"):
            continue
        from_id = lk.get("from_task_id")
        if not from_id:
            continue
        out_links.setdefault(from_id, []).append(lk)

    children: list[ChildState] = []
    seen: set[str] = {root_task_id}
    # BFS over drives out-edges. (task_id, depth)
    frontier: list[tuple[str, int]] = [(root_task_id, 0)]
    while frontier:
        parent_id, depth = frontier.pop(0)
        for lk in out_links.get(parent_id, []):
            to_id = lk.get("to_task_id")
            if not to_id:
                continue
            task = tasks_by_id.get(to_id, {})
            child = ChildState(
                task_id=to_id,
                title=task.get("title", "") or "",
                status=task.get("status", "unknown") or "unknown",
                kind="drives",
                link_id=lk.get("id"),
                parent_task_id=parent_id,
                depth=depth + 1,
            )
            if to_id not in seen:
                children.append(child)
                seen.add(to_id)
                # Every drives edge extends the subtree (recursion is
                # bounded by depth/fan-out caps in the engine, not here).
                frontier.append((to_id, depth + 1))
    return SubtreeView(
        root_task_id=root_task_id,
        bundle_id=bundle_id,
        children=tuple(children),
    )


async def reconcile_subtree(
    client: "KrewHubClient",
    root_task_id: str,
    bundle_id: str,
) -> SubtreeView:
    """Fetch the bundle's tasks + links and build the current subtree view.

    Reliable level-triggered path (mirrors the daemon's poll fallback).
    Raises nothing fatal: on a transport hiccup it returns an empty
    subtree so the loop keeps ticking rather than crashing.
    """
    try:
        bundle_detail = await client.get_bundle(bundle_id)
        links = await client.get_bundle_links(bundle_id)
    except Exception:
        logger.warning(
            "orch: reconcile failed for root %s (bundle %s)",
            root_task_id, bundle_id, exc_info=True,
        )
        return SubtreeView(root_task_id=root_task_id, bundle_id=bundle_id, children=())

    tasks_by_id = _index_tasks(bundle_detail)
    return build_subtree(root_task_id, bundle_id, tasks_by_id, links)
