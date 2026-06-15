"""Unit tests for orch subtree reconcile (gap 5, C5)."""

from __future__ import annotations

import pytest

from krewcli.daemon.orch_subtree import (
    TERMINAL_STATUSES,
    build_subtree,
    reconcile_subtree,
)


def _task(tid, status="open", title="", created_by=None):
    return {"id": tid, "status": status, "title": title, "created_by_task": created_by}


def _link(lid, frm, to, kind="subagent", created_by=None, revoked=None):
    return {
        "id": lid,
        "from_task_id": frm,
        "to_task_id": to,
        "kind": kind,
        "created_by_task": created_by if created_by is not None else frm,
        "revoked_at": revoked,
    }


def test_direct_children_from_subagent_links():
    tasks = {
        "A": _task("A", "working"),
        "B": _task("B", "open"),
        "C": _task("C", "done"),
    }
    links = [_link("l1", "A", "B"), _link("l2", "A", "C")]
    view = build_subtree("A", "BND", tasks, links)

    ids = {c.task_id for c in view.children}
    assert ids == {"B", "C"}
    assert all(c.depth == 1 for c in view.children)
    assert {c.task_id: c.status for c in view.children} == {"B": "open", "C": "done"}


def test_grandchildren_walk_provenance():
    tasks = {
        "A": _task("A"), "B": _task("B"), "C": _task("C", "blocked"),
    }
    # A -> B (subagent), B -> C (subagent, created_by B)
    links = [_link("l1", "A", "B"), _link("l2", "B", "C", created_by="B")]
    view = build_subtree("A", "BND", tasks, links)

    by_id = {c.task_id: c for c in view.children}
    assert set(by_id) == {"B", "C"}
    assert by_id["B"].depth == 1
    assert by_id["C"].depth == 2  # grandchild reached via provenance


def test_revoked_links_excluded():
    tasks = {"A": _task("A"), "B": _task("B")}
    links = [_link("l1", "A", "B", revoked="2026-06-15T00:00:00Z")]
    view = build_subtree("A", "BND", tasks, links)
    assert view.children == ()


def test_every_drives_edge_extends_the_subtree():
    # v3: a single "drives" link primitive — there is no pipe-vs-subagent
    # distinction. Every out-edge extends the subtree, so a grandchild
    # reached through any chain of drives links is owned.
    tasks = {"A": _task("A"), "B": _task("B"), "C": _task("C")}
    links = [
        _link("l1", "A", "B"),
        _link("l2", "B", "C", created_by="B"),
    ]
    view = build_subtree("A", "BND", tasks, links)
    ids = {c.task_id for c in view.children}
    assert ids == {"B", "C"}
    assert all(c.kind == "drives" for c in view.children)


def test_terminal_signature_only_terminal_children():
    tasks = {
        "A": _task("A"),
        "B": _task("B", "working"),
        "C": _task("C", "done"),
        "D": _task("D", "blocked"),
    }
    links = [_link("l1", "A", "B"), _link("l2", "A", "C"), _link("l3", "A", "D")]
    view = build_subtree("A", "BND", tasks, links)

    sig = view.terminal_signature()
    assert sig == frozenset({("C", "done"), ("D", "blocked")})
    assert not view.all_done()  # B still working
    assert {c.task_id for c in view.pending()} == {"B"}


def test_all_done_true_when_every_child_done():
    tasks = {"A": _task("A"), "B": _task("B", "done"), "C": _task("C", "done")}
    links = [_link("l1", "A", "B"), _link("l2", "A", "C")]
    view = build_subtree("A", "BND", tasks, links)
    assert view.all_done()
    assert set(TERMINAL_STATUSES) == {"done", "blocked", "cancelled"}


@pytest.mark.asyncio
async def test_reconcile_subtree_fetches_and_builds():
    class _Client:
        async def get_bundle(self, bundle_id):
            assert bundle_id == "BND"
            return {"tasks": [_task("A", "working"), _task("B", "done")]}

        async def get_bundle_links(self, bundle_id):
            return [_link("l1", "A", "B")]

    view = await reconcile_subtree(_Client(), "A", "BND")
    assert {c.task_id for c in view.children} == {"B"}
    assert view.all_done()


@pytest.mark.asyncio
async def test_reconcile_subtree_tolerates_transport_error():
    class _Client:
        async def get_bundle(self, bundle_id):
            raise RuntimeError("boom")

        async def get_bundle_links(self, bundle_id):
            return []

    view = await reconcile_subtree(_Client(), "A", "BND")
    assert view.children == ()  # empty, no crash
