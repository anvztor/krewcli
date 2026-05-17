"""Shared fixtures for mcp_servers tests."""
import pytest
from unittest.mock import AsyncMock

from krewcli.mcp_servers.bridge import BridgeSession
from krewcli.mcp_servers import hitl_tool as hitl_module


@pytest.fixture
def bridge_for_task(monkeypatch):
    """A BridgeSession bound to task_id='test_task_123'.

    The underlying HitlTool's emitter/resolver are replaced with
    AsyncMock instances so tests don't hit real krewhub.
    """
    fake_emitter = type("FakeEmitter", (), {
        "emit_elicit": AsyncMock(return_value="el_test"),
    })()
    fake_resolver = type("FakeResolver", (), {
        "wait_for_resolution": AsyncMock(return_value={"action": "accept"}),
    })()

    session = BridgeSession(task_id="test_task_123")
    # Replace the hitl instance on this session with one using fakes.
    session._hitl = hitl_module.HitlTool(
        emitter=fake_emitter,
        resolver=fake_resolver,
        task_id_provider=lambda: "test_task_123",
    )
    return session
