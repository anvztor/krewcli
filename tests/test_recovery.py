from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from krewcli.daemon.recovery import recover_orphans
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.daemon.loop import DaemonLoop
from krewcli.backend.echo import EchoBackend
import asyncio

@pytest.mark.asyncio
async def test_recover_orphans_success():
    client = AsyncMock(spec=KrewHubClient)
    client.get_working_tasks.return_value = [
        {"id": "task_1", "claimed_by_agent_id": "agent_1"},
        {"id": "task_2", "claimed_by_agent_id": "agent_2"}, # Ignored
        {"id": "task_3", "claimed_by_agent_id": "agent_1"},
    ]
    
    recovered = await recover_orphans(client, ["agent_1"])
    
    assert recovered == 2
    client.update_task_status.assert_any_call(
        "task_1",
        status="blocked",
        blocked_reason="daemon_crash_recovery: task was in-flight when daemon stopped"
    )
    client.update_task_status.assert_any_call(
        "task_3",
        status="blocked",
        blocked_reason="daemon_crash_recovery: task was in-flight when daemon stopped"
    )
    
@pytest.mark.asyncio
async def test_recover_orphans_failure():
    client = AsyncMock(spec=KrewHubClient)
    client.get_working_tasks.return_value = [
        {"id": "task_1", "claimed_by_agent_id": "agent_1"},
    ]
    client.update_task_status.side_effect = Exception("Network error")
    
    recovered = await recover_orphans(client, ["agent_1"])
    
    assert recovered == 0

@pytest.mark.asyncio
async def test_recovery_path_fails_intentionally():
    """An e2e test for the recovery path that fails intentionally."""
    client = AsyncMock(spec=KrewHubClient)
    client._client = MagicMock()
    client._client.base_url = "http://fake"
    
    # Mock working tasks
    client.get_working_tasks.return_value = [
        {"id": "task_orphan_1", "claimed_by_agent_id": "echo@fake"}
    ]
    
    backends = {"echo": EchoBackend()}
    loop = DaemonLoop(
        client=client,
        backends=backends,
        cookbook_id="cb",
        recipe_id="rec",
        working_dir="/tmp"
    )
    
    loop._owner = "fake"
    loop._agent_ids = {"echo": "echo@fake"}
    
    with patch("krewcli.auth.token_store.load_token", return_value="fake_token"):
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
            
    client.update_task_status.assert_called_with(
        "task_orphan_1", status="blocked", blocked_reason="daemon_crash_recovery: task was in-flight when daemon stopped"
    )
    
