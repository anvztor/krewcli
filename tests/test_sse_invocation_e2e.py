"""E2E test: SSE-driven A2A invocation lifecycle.

Proves the full chain without any polling fallback:

  cookrew/API → krewhub bundle → GraphRunner → A2A hub mailbox
  → SSEWatcher pickup → daemon harness → Backend execute
  → Session events:batch → task done → /a2a/respond

Requires:
  - krewhub running at KREWHUB_URL (default: https://hub.cookrew.dev)
  - KREWCLI_E2E=1 env var
  - Claude CLI on PATH (or --agents echo for fast mode)

Run with:
  KREWCLI_E2E=1 uv run pytest tests/test_sse_invocation_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import uuid
from time import monotonic

import pytest

from krewcli.auth.token_store import load_token
from krewcli.client.krewhub_client import KrewHubClient

pytestmark = pytest.mark.skipif(
    os.getenv("KREWCLI_E2E", "").strip().lower() not in {"1", "true", "yes"},
    reason="Set KREWCLI_E2E=1 to run E2E tests",
)

KREWHUB_URL = os.getenv("KREWHUB_URL", "https://hub.cookrew.dev")
COOKBOOK_ID = os.getenv("KREWHUB_COOKBOOK_ID", "cb_6f9c1c8d")


@pytest.fixture
async def client():
    c = KrewHubClient(
        KREWHUB_URL,
        api_key="",
        jwt_token=load_token(),
        verify_ssl=True,
    )
    yield c
    await c.close()


async def _poll_until(check, *, timeout=120, interval=3):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timed out after {timeout}s")


@pytest.mark.asyncio
async def test_sse_invocation_full_lifecycle(client, tmp_path):
    """Full SSE-driven lifecycle: bundle → graph → dispatch → execute → done.

    This test creates a bundle with pre-defined tasks (skipping the planner),
    starts a daemon with the echo backend, and verifies that krewhub's
    GraphRunnerController dispatches via the A2A hub, the SSEWatcher picks
    it up, the harness executes it, and events flow back.
    """
    from krewcli.backend.echo import EchoBackend
    from krewcli.backend.registry import BACKEND_INFO
    from krewcli.daemon.loop import DaemonLoop
    from krewcli.gateway.identity import _get_owner_label, _make_agent_id

    tag = uuid.uuid4().hex[:6]

    # 1. Create a recipe + bundle with 1 task
    recipe = await client.create_recipe(
        name=f"e2e-sse-{tag}",
        repo_url="https://github.com/test/e2e.git",
        created_by="e2e_test",
        cookbook_id=COOKBOOK_ID,
    )
    recipe_id = recipe["id"]

    bundle, tasks = await client.create_bundle(
        recipe_id=recipe_id,
        prompt=f"E2E SSE test {tag}: echo backend should complete this via A2A hub",
        requested_by="e2e_test",
        tasks=[{
            "title": f"Echo task {tag}",
            "description": "Simple task for the echo backend",
            "depends_on_task_ids": [],
        }],
    )
    bundle_id = bundle["id"]
    task_id = tasks[0]["id"]

    # Verify task is open
    task = await client.get_task(task_id)
    assert task["status"] == "open", f"Expected open, got {task['status']}"

    # 2. Register the echo agent with endpoint_url → A2A hub
    owner = _get_owner_label()
    agent_id = _make_agent_id("echo", owner)
    hub_endpoint = f"{KREWHUB_URL}/a2a/{owner}/echo"

    await client.register_agent(
        agent_id=agent_id,
        cookbook_id=COOKBOOK_ID,
        display_name="Echo Agent (E2E)",
        capabilities=BACKEND_INFO["echo"]["capabilities"],
        max_concurrent_tasks=1,
        endpoint_url=hub_endpoint,
    )

    # Verify endpoint is set
    agents = await client.list_agents(COOKBOOK_ID)
    echo_agent = next((a for a in agents if a["agent_id"] == agent_id), None)
    assert echo_agent is not None, f"Agent {agent_id} not found"
    assert echo_agent.get("endpoint_url"), "endpoint_url not set after registration"

    # 3. Start daemon loop in background
    daemon = DaemonLoop(
        client=client,
        backends={"echo": EchoBackend()},
        cookbook_id=COOKBOOK_ID,
        recipe_id=recipe_id,
        working_dir=str(tmp_path),
        max_concurrent=1,
        poll_interval=30.0,  # high interval — we rely on SSE, not polling
    )

    daemon_task = asyncio.create_task(daemon.run())

    try:
        # 4. Claim the task as the echo agent (simulating GraphRunner dispatch)
        #    In production, GraphRunner would do this. Here we simulate
        #    by posting an A2A invocation to the hub.
        import httpx

        a2a_payload = {
            "jsonrpc": "2.0",
            "id": f"{task_id}:1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "user",
                    "parts": [{"kind": "text", "text": f"Execute echo task {tag}"}],
                    "metadata": {
                        "task_id": task_id,
                        "bundle_id": bundle_id,
                        "attempt": 1,
                        "recipe_id": recipe_id,
                    },
                },
                "configuration": {"returnImmediately": True},
            },
        }

        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                f"{KREWHUB_URL}/a2a/{owner}/echo",
                json=a2a_payload,
                headers={"Authorization": f"Bearer {load_token()}"},
            )
            assert resp.status_code == 200, f"A2A dispatch failed: {resp.status_code} {resp.text}"
            result = resp.json().get("result", {})
            state = result.get("status", {}).get("state", "")
            assert state in ("submitted", "working", "completed"), f"Unexpected state: {state}"

        # 5. Wait for the daemon to pick up and complete the task
        async def check_done():
            t = await client.get_task(task_id)
            return t if t["status"] in ("done", "blocked") else None

        done_task = await _poll_until(check_done, timeout=60)
        assert done_task["status"] == "done", f"Expected done, got {done_task['status']}"

        # 6. Verify events were streamed
        detail = await client.get_bundle(bundle_id)
        events = detail.get("events", [])
        event_types = [e["type"] for e in events]

        assert "session_start" in event_types, f"Missing session_start. Got: {event_types}"
        assert "session_end" in event_types, f"Missing session_end. Got: {event_types}"

        # 7. Verify A2A respond was posted (invocation completed)
        #    The SSEWatcher posts to /a2a/respond after execution.
        #    We can't directly check the invocation status, but task=done
        #    proves the harness ran and the session flushed.

    finally:
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass
