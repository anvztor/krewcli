"""Verify hitl.request_access is in tools/list and the bridge wires it correctly."""
import pytest


@pytest.mark.asyncio
async def test_hitl_in_tools_list(bridge_for_task):
    """When a bridge is instantiated for a task, tools/list includes hitl.request_access."""
    tools = await bridge_for_task.list_tools()
    names = [t["name"] for t in tools]
    assert "hitl.request_access" in names


@pytest.mark.asyncio
async def test_hitl_call_routes_to_handler(bridge_for_task, monkeypatch):
    """tools/call with name=hitl.request_access reaches the handler."""
    from krewcli.mcp_servers import hitl_tool as hitl_module
    seen = {}
    async def fake_handler(self, **kwargs):
        seen.update(kwargs)
        return {"status": "granted", "retry_hint": "x"}
    monkeypatch.setattr(hitl_module.HitlTool, "request_access", fake_handler, raising=True)

    result = await bridge_for_task.call_tool(
        "hitl.request_access",
        arguments={"provider": "github", "reason": "test"},
    )
    assert result.get("status") == "granted"
    assert seen.get("provider") == "github"
