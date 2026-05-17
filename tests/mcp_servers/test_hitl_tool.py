import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from krewcli.mcp_servers.hitl_tool import HitlTool


class _FakeEmitter:
    def __init__(self):
        self.emit_elicit = AsyncMock(return_value="el_123")


class _FakeResolver:
    def __init__(self, result):
        self._result = result
        self.wait_for_resolution = AsyncMock(return_value=result)


@pytest.mark.asyncio
async def test_granted():
    emitter = _FakeEmitter()
    resolver = _FakeResolver({"action": "accept"})
    tool = HitlTool(
        emitter=emitter, resolver=resolver,
        task_id_provider=lambda: "task_xyz",
    )
    r = await tool.request_access(provider="github", reason="need to clone")
    emitter.emit_elicit.assert_awaited_once()
    payload = emitter.emit_elicit.call_args.kwargs["payload"]
    assert payload["op"] == "auth_required"
    assert payload["provider"] == "github"
    assert payload["reason"] == "need to clone"
    assert r["status"] == "granted"


@pytest.mark.asyncio
async def test_denied_on_reject():
    tool = HitlTool(
        emitter=_FakeEmitter(),
        resolver=_FakeResolver({"action": "reject"}),
        task_id_provider=lambda: "t",
    )
    r = await tool.request_access(provider="github", reason="x")
    assert r["status"] == "denied"


@pytest.mark.asyncio
async def test_timeout():
    emitter = _FakeEmitter()

    class _HangingResolver:
        async def wait_for_resolution(self, *_a, **_kw):
            await asyncio.sleep(10)
            return {"action": "accept"}

    tool = HitlTool(
        emitter=emitter, resolver=_HangingResolver(),
        task_id_provider=lambda: "t",
        default_timeout_s=0.05,
    )
    r = await tool.request_access(provider="github", reason="x")
    assert r["status"] == "timeout"


@pytest.mark.asyncio
async def test_budget_keyed_on_server_task_id_not_tool_arg():
    """codex v3 BLOCKER #6: the agent must NOT be able to bypass the
    per-task budget by varying any tool arg."""
    emitter = _FakeEmitter()
    resolver = _FakeResolver({"action": "accept"})
    tool = HitlTool(
        emitter=emitter, resolver=resolver,
        task_id_provider=lambda: "task_x",
        max_grants_per_provider=2,
    )
    r1 = await tool.request_access(provider="github", reason="r1")
    r2 = await tool.request_access(provider="github", reason="r2")
    r3 = await tool.request_access(provider="github", reason="r3")
    assert r1["status"] == "granted"
    assert r2["status"] == "granted"
    assert r3["status"] == "denied"
    assert "budget" in r3.get("reason", "").lower()


@pytest.mark.asyncio
async def test_budget_resets_per_provider():
    emitter = _FakeEmitter()
    resolver = _FakeResolver({"action": "accept"})
    tool = HitlTool(
        emitter=emitter, resolver=resolver,
        task_id_provider=lambda: "t",
        max_grants_per_provider=1,
    )
    g1 = await tool.request_access(provider="github", reason="r")
    g2 = await tool.request_access(provider="github", reason="r")  # exhausts github
    o1 = await tool.request_access(provider="openai", reason="r")  # openai is fresh
    assert g1["status"] == "granted"
    assert g2["status"] == "denied"
    assert o1["status"] == "granted"


@pytest.mark.asyncio
async def test_resource_param_optional_and_passed_through():
    emitter = _FakeEmitter()
    resolver = _FakeResolver({"action": "accept"})
    tool = HitlTool(
        emitter=emitter, resolver=resolver,
        task_id_provider=lambda: "t",
    )
    await tool.request_access(
        provider="github", reason="x", resource="org/repo",
    )
    payload = emitter.emit_elicit.call_args.kwargs["payload"]
    assert payload.get("resource") == "org/repo"
