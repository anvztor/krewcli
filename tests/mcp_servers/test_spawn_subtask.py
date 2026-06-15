"""Tests for the spawn_subtask bridge tool (gap 5, C3 — eval E1).

spawn_subtask is the orch-agent's decompose primitive: it POSTs an inline
``new_task`` to ``/api/v1/tasks/{A}/links`` with ``kind="subagent"``,
creating a provenance-stamped child + subagent link in one call.
"""

from __future__ import annotations

import json

import pytest


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


def _install_fake_post(monkeypatch, bridge, capture, *, status=200, body=None):
    body = body if body is not None else {
        "link": {"id": "lnk_abc", "to_task_id": "task_B", "kind": "subagent",
                 "created_by_task": "task_A"},
        "to_task": {"id": "task_B", "title": "child B", "status": "open"},
    }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            capture.append({"url": url, "json": json, "headers": headers})
            return _FakeResponse(status, body)

    monkeypatch.setattr(bridge.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_spawn_subtask_posts_new_task_link(monkeypatch):
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok123")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_A")

    capture: list[dict] = []
    _install_fake_post(monkeypatch, bridge, capture)

    result = await bridge.spawn_subtask({
        "title": "child B",
        "brief": {"goal": "do B", "deliverable": "a PR"},
    })

    # E1: one call to POST /tasks/A/links with new_task + subagent kind.
    assert len(capture) == 1
    call = capture[0]
    assert call["url"] == "http://krewhub:8420/api/v1/tasks/task_A/links"
    assert call["json"]["kind"] == "subagent"
    assert call["json"]["new_task"]["title"] == "child B"
    assert call["json"]["new_task"]["brief"] == {"goal": "do B", "deliverable": "a PR"}
    assert call["headers"]["Authorization"] == "Bearer tok123"

    # Returns the new task_id + link_id the brain can act on.
    assert result == {
        "ok": True, "task_id": "task_B", "link_id": "lnk_abc", "title": "child B",
    }


@pytest.mark.asyncio
async def test_spawn_subtask_parent_from_env_not_args(monkeypatch):
    """The brain cannot re-parent: parent id comes only from env."""
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_REAL")

    capture: list[dict] = []
    _install_fake_post(monkeypatch, bridge, capture)

    # Attacker tries to inject a different parent via args — ignored.
    await bridge.spawn_subtask({
        "title": "x", "brief": {"goal": "g", "deliverable": "d"},
        "task_id": "task_VICTIM", "from_task_id": "task_VICTIM",
    })
    assert capture[0]["url"].endswith("/tasks/task_REAL/links")


@pytest.mark.asyncio
async def test_spawn_subtask_requires_parent(monkeypatch):
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.delenv("KREWHUB_TASK_ID", raising=False)

    result = await bridge.spawn_subtask({
        "title": "x", "brief": {"goal": "g", "deliverable": "d"},
    })
    assert result["ok"] is False
    assert "no_parent_task" in result["error"]


@pytest.mark.asyncio
async def test_spawn_subtask_validates_brief(monkeypatch):
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_A")

    # Missing deliverable.
    result = await bridge.spawn_subtask({"title": "x", "brief": {"goal": "g"}})
    assert result["ok"] is False
    assert "deliverable" in result["error"]

    # Missing title.
    result = await bridge.spawn_subtask({"brief": {"goal": "g", "deliverable": "d"}})
    assert result["ok"] is False
    assert "title" in result["error"]


@pytest.mark.asyncio
async def test_spawn_subtask_surfaces_http_error(monkeypatch):
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_A")

    capture: list[dict] = []
    _install_fake_post(monkeypatch, bridge, capture, status=400,
                       body={"detail": "link would create a dependency cycle"})

    result = await bridge.spawn_subtask({
        "title": "x", "brief": {"goal": "g", "deliverable": "d"},
    })
    assert result["ok"] is False
    assert "create_link_failed: 400" in result["error"]


@pytest.mark.asyncio
async def test_tools_list_always_exposes_spawn(monkeypatch):
    """v3: every task's brain may decompose, so spawn_subtask is always
    advertised (no orch-mode gate)."""
    from krewcli.mcp_servers import bridge

    monkeypatch.delenv("KREWCLI_ORCH_AGENT", raising=False)
    resp = await bridge.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"delegate", "hitl.request_access", "spawn_subtask"} <= names


@pytest.mark.asyncio
async def test_spawn_budget_caps_spawns_per_turn(monkeypatch):
    """Blast-radius cap: spawn_subtask refuses once the per-turn budget is
    spent (the cap that bounds an injection/loop-driven runaway)."""
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_A")
    monkeypatch.setenv("KREWCLI_ORCH_SPAWN_BUDGET", "1")
    monkeypatch.setattr(bridge, "_spawn_count", 0, raising=False)

    capture: list[dict] = []
    _install_fake_post(monkeypatch, bridge, capture)

    first = await bridge.spawn_subtask({"title": "B", "brief": {"goal": "g", "deliverable": "d"}})
    second = await bridge.spawn_subtask({"title": "C", "brief": {"goal": "g", "deliverable": "d"}})
    assert first["ok"] is True
    assert second["ok"] is False
    assert "spawn_budget_exhausted" in second["error"]
    assert len(capture) == 1  # only the first reached krewhub


@pytest.mark.asyncio
async def test_tools_call_routes_spawn_subtask(monkeypatch):
    from krewcli.mcp_servers import bridge

    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_A")
    monkeypatch.setenv("KREWCLI_ORCH_AGENT", "1")

    capture: list[dict] = []
    _install_fake_post(monkeypatch, bridge, capture)

    resp = await bridge.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "spawn_subtask",
            "arguments": {"title": "child B",
                          "brief": {"goal": "do B", "deliverable": "a PR"}},
        },
    })
    envelope = json.loads(resp["result"]["content"][0]["text"])
    assert envelope["ok"] is True
    assert envelope["task_id"] == "task_B"
    assert resp["result"]["isError"] is False
