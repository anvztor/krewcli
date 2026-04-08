"""Tests for KrewHubClient.attach_graph + PlannerOrchestratorExecutor.

Two layers:
    - KrewHubClient.attach_graph: mocks httpx via respx-style AsyncMock
      to verify request shape + status code mapping.
    - PlannerOrchestratorExecutor: drives execute() with a fake context
      and asserts the right A2A events fire and the krewhub client is
      called with the generated code.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from a2a.types import TaskState

from krewcli.a2a.executors.planner_agent import (
    PlannerOrchestratorExecutor,
    build_planner_card,
)
from krewcli.client.krewhub_client import KrewHubClient


# ---------------------------------------------------------------------------
# KrewHubClient.attach_graph
# ---------------------------------------------------------------------------


def _make_response(status_code: int, body: dict | None = None) -> Mock:
    resp = Mock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=Mock(), response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestAttachGraphClient:
    @pytest.mark.asyncio
    async def test_happy_path_returns_bundle_and_tasks(self):
        client = KrewHubClient("http://hub", "test-key")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post.return_value = _make_response(
            200,
            {
                "bundle": {"id": "b1", "graph_code": "...", "graph_mermaid": "flowchart LR"},
                "tasks": [{"id": "t1", "graph_node_id": "step1"}],
            },
        )

        result = await client.attach_graph("b1", "code...", created_by="orchestrator")

        assert result["bundle"]["id"] == "b1"
        assert result["tasks"][0]["graph_node_id"] == "step1"

        # Verify URL + payload shape
        call = client._client.post.call_args
        assert call.args[0] == "/api/v1/bundles/b1/graph"
        assert call.kwargs["json"] == {"code": "code...", "created_by": "orchestrator"}

    @pytest.mark.asyncio
    async def test_404_raises(self):
        client = KrewHubClient("http://hub", "test-key")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post.return_value = _make_response(404, {"detail": "bundle not found"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.attach_graph("missing", "code")

    @pytest.mark.asyncio
    async def test_409_raises(self):
        client = KrewHubClient("http://hub", "test-key")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post.return_value = _make_response(409, {"detail": "already attached"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.attach_graph("b1", "code")

    @pytest.mark.asyncio
    async def test_422_raises(self):
        client = KrewHubClient("http://hub", "test-key")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post.return_value = _make_response(422, {"detail": "code rejected"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.attach_graph("b1", "bad code")

    @pytest.mark.asyncio
    async def test_default_created_by_is_orchestrator(self):
        client = KrewHubClient("http://hub", "test-key")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post.return_value = _make_response(200, {"bundle": {}, "tasks": []})
        await client.attach_graph("b1", "code")
        call = client._client.post.call_args
        assert call.kwargs["json"]["created_by"] == "orchestrator"


# ---------------------------------------------------------------------------
# PlannerOrchestratorExecutor
# ---------------------------------------------------------------------------


def _make_context(
    *,
    text: str = "Build something",
    bundle_id: str | None = "bun_test",
    cookbook_id: str | None = None,
) -> SimpleNamespace:
    """Construct a fake RequestContext sufficient for executor.execute()."""
    metadata: dict[str, Any] = {}
    if bundle_id is not None:
        metadata["bundle_id"] = bundle_id
    if cookbook_id is not None:
        metadata["cookbook_id"] = cookbook_id

    text_part = SimpleNamespace(root=SimpleNamespace(text=text))
    message = SimpleNamespace(
        message_id="msg_1",
        role="user",
        parts=[text_part],
        metadata=metadata or None,
    )
    # Pre-populate current_task so the executor's `current_task or new_task()`
    # short-circuits and we don't have to satisfy the a2a SDK's strict
    # message → Task validation in tests.
    fake_task = SimpleNamespace(id="a2a_task_1")
    return SimpleNamespace(
        request=SimpleNamespace(message=message),
        message=message,
        current_task=fake_task,
        task_id="a2a_task_1",
        context_id="a2a_ctx_1",
    )


def _collect_queue() -> tuple[AsyncMock, list[Any]]:
    events: list[Any] = []
    queue = AsyncMock()
    queue.enqueue_event = AsyncMock(side_effect=lambda evt: events.append(evt))
    return queue, events


def _final_state(events: list[Any]) -> TaskState | None:
    """Return the state of the final TaskStatusUpdateEvent if any."""
    for evt in reversed(events):
        if hasattr(evt, "status") and getattr(evt, "final", False):
            return evt.status.state
    return None


def _final_message_text(events: list[Any]) -> str:
    for evt in reversed(events):
        if hasattr(evt, "status") and getattr(evt, "final", False):
            msg = evt.status.message
            if msg and getattr(msg, "parts", None):
                for part in msg.parts:
                    if hasattr(part, "root") and hasattr(part.root, "text"):
                        return part.root.text
                    if hasattr(part, "text"):
                        return part.text
    return ""


def _artifact_text(events: list[Any]) -> str | None:
    for evt in events:
        if hasattr(evt, "artifact"):
            for part in evt.artifact.parts:
                if hasattr(part, "root") and hasattr(part.root, "text"):
                    return part.root.text
                if hasattr(part, "text"):
                    return part.text
    return None


class TestPlannerExecutor:
    @pytest.mark.asyncio
    async def test_happy_path_generates_and_attaches(self):
        hub = AsyncMock(spec=KrewHubClient)
        hub.list_agents.return_value = [
            {"agent_id": "gw1", "endpoint_url": "http://gw1", "status": "online"},
        ]
        hub.attach_graph.return_value = {
            "bundle": {"id": "bun_test", "graph_mermaid": "flowchart LR"},
            "tasks": [
                {"id": "t1", "graph_node_id": "step_a"},
                {"id": "t2", "graph_node_id": "step_b"},
            ],
        }

        async def fake_generator(prompt, agents, agent_endpoints):
            assert prompt == "Build something"
            assert "gw1" in agent_endpoints
            return "g = GraphBuilder(...); graph = g.build()"

        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub,
            cookbook_id="cb_test",
            code_generator=fake_generator,
        )
        ctx = _make_context()
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.completed
        hub.attach_graph.assert_awaited_once()
        call = hub.attach_graph.await_args
        assert call.args[0] == "bun_test"
        assert call.args[1] == "g = GraphBuilder(...); graph = g.build()"

        artifact_text = _artifact_text(events)
        assert artifact_text is not None
        summary = json.loads(artifact_text)
        assert summary["bundle_id"] == "bun_test"
        assert summary["task_count"] == 2
        assert set(summary["node_ids"]) == {"step_a", "step_b"}

    @pytest.mark.asyncio
    async def test_missing_bundle_id_fails_fast(self):
        hub = AsyncMock(spec=KrewHubClient)
        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="cb",
            code_generator=AsyncMock(return_value="code"),
        )
        ctx = _make_context(bundle_id=None)
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.failed
        assert "bundle_id" in _final_message_text(events)
        hub.list_agents.assert_not_called()
        hub.attach_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_prompt_fails_fast(self):
        hub = AsyncMock(spec=KrewHubClient)
        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="cb",
            code_generator=AsyncMock(return_value="code"),
        )
        ctx = _make_context(text="")
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.failed
        assert "prompt" in _final_message_text(events).lower()

    @pytest.mark.asyncio
    async def test_no_online_gateways_fails(self):
        hub = AsyncMock(spec=KrewHubClient)
        hub.list_agents.return_value = [
            {"agent_id": "gw1", "endpoint_url": None, "status": "online"},
            {"agent_id": "gw2", "endpoint_url": "http://gw2", "status": "offline"},
        ]
        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="cb",
            code_generator=AsyncMock(return_value="code"),
        )
        ctx = _make_context()
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.failed
        assert "no online gateways" in _final_message_text(events).lower()
        hub.attach_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_code_generator_returns_none_fails(self):
        hub = AsyncMock(spec=KrewHubClient)
        hub.list_agents.return_value = [
            {"agent_id": "gw1", "endpoint_url": "http://gw1", "status": "online"},
        ]
        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="cb",
            code_generator=AsyncMock(return_value=None),
        )
        ctx = _make_context()
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.failed
        assert "no output" in _final_message_text(events).lower()
        hub.attach_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_graph_422_fails_with_detail(self):
        hub = AsyncMock(spec=KrewHubClient)
        hub.list_agents.return_value = [
            {"agent_id": "gw1", "endpoint_url": "http://gw1", "status": "online"},
        ]
        bad_response = _make_response(422, {"detail": "code rejected: import os banned"})
        hub.attach_graph.side_effect = httpx.HTTPStatusError(
            "boom", request=Mock(), response=bad_response,
        )
        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="cb",
            code_generator=AsyncMock(return_value="bad code"),
        )
        ctx = _make_context()
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.failed
        msg = _final_message_text(events)
        assert "422" in msg
        assert "rejected" in msg.lower()

    @pytest.mark.asyncio
    async def test_attach_graph_network_error_fails(self):
        hub = AsyncMock(spec=KrewHubClient)
        hub.list_agents.return_value = [
            {"agent_id": "gw1", "endpoint_url": "http://gw1", "status": "online"},
        ]
        hub.attach_graph.side_effect = httpx.ConnectError("connection refused")
        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="cb",
            code_generator=AsyncMock(return_value="code"),
        )
        ctx = _make_context()
        queue, events = _collect_queue()

        await executor.execute(ctx, queue)

        assert _final_state(events) == TaskState.failed
        assert "unreachable" in _final_message_text(events).lower()

    @pytest.mark.asyncio
    async def test_cookbook_id_metadata_overrides_constructor(self):
        hub = AsyncMock(spec=KrewHubClient)
        hub.list_agents.return_value = [
            {"agent_id": "gw1", "endpoint_url": "http://gw1", "status": "online"},
        ]
        hub.attach_graph.return_value = {"bundle": {}, "tasks": []}

        async def gen(p, a, e):
            return "code"

        executor = PlannerOrchestratorExecutor(
            krewhub_client=hub, cookbook_id="default_cb",
            code_generator=gen,
        )
        ctx = _make_context(cookbook_id="override_cb")
        queue, _events = _collect_queue()

        await executor.execute(ctx, queue)

        hub.list_agents.assert_awaited_once_with("override_cb")


# ---------------------------------------------------------------------------
# Agent card
# ---------------------------------------------------------------------------


class TestPlannerCard:
    def test_card_advertises_generate_graph_skill(self):
        card = build_planner_card("127.0.0.1", 9000)
        assert card.name == "planner"
        assert card.url == "http://127.0.0.1:9000"
        assert len(card.skills) == 1
        assert card.skills[0].id == "generate-graph"
        assert "planner" in card.skills[0].tags
