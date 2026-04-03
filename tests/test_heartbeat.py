from __future__ import annotations

from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.client.krewhub_client import KrewHubClient


def test_heartbeat_current_task_property():
    client = KrewHubClient("http://fake:1234", "key")
    heartbeat = HeartbeatLoop(
        client=client,
        agent_id="test_agent",
        cookbook_id="cb_1",
        display_name="Test Agent",
        capabilities=["claim"],
        interval=60,
    )
    assert heartbeat.current_task_id is None
    heartbeat.current_task_id = "task_123"
    assert heartbeat.current_task_id == "task_123"
    heartbeat.current_task_id = None
    assert heartbeat.current_task_id is None
