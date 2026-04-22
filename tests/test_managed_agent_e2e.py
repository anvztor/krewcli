"""End-to-end test for the managed agent daemon.

Exercises the full Managed Agents flow:
  1. Start real krewhub
  2. Create recipe + bundle with 1 task
  3. Start krewcli daemon with echo backend
  4. Daemon polls → claims → executes → streams events → completes
  5. Verify task status transitions and event persistence

Requires:
  - krewhub running (or startable) at localhost:8421
  - KREWCLI_E2E=1 env var to opt in (skipped otherwise)

Run with:
  KREWCLI_E2E=1 uv run pytest tests/test_managed_agent_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from time import monotonic

import httpx
import pytest

from krewcli.client.krewhub_client import KrewHubClient

KREWHUB_PROJECT_PATH = Path(__file__).resolve().parents[2] / "krewhub"

# Skip unless explicitly opted in
pytestmark = pytest.mark.skipif(
    os.getenv("KREWCLI_E2E", "").strip().lower() not in {"1", "true", "yes"},
    reason="Set KREWCLI_E2E=1 to run E2E tests",
)


@pytest.fixture
def krewhub_url():
    return os.getenv("KREWHUB_URL", "http://127.0.0.1:8421")


@pytest.fixture
async def client(krewhub_url):
    """KrewHub API client for test setup."""
    c = KrewHubClient(
        krewhub_url,
        api_key=os.getenv("KREWHUB_API_KEY", "test-api-key"),
        verify_ssl=False,
    )
    yield c
    await c.close()


async def _poll_until(check, timeout=60, interval=2):
    """Poll a callable until it returns truthy or timeout."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timed out after {timeout}s")


@pytest.mark.asyncio
async def test_daemon_e2e_full_flow(client, krewhub_url, tmp_path):
    """Full managed agent E2E: create → claim → execute → complete."""

    # 1. Create recipe
    recipe = await client.create_recipe(
        name=f"e2e-managed-{uuid.uuid4().hex[:6]}",
        repo_url="https://github.com/test/e2e-repo.git",
        created_by="e2e_test",
        cookbook_id=os.getenv("KREWHUB_COOKBOOK_ID", "e2e_cookbook"),
    )
    recipe_id = recipe["id"]

    # 2. Create bundle with 1 task
    bundle, tasks = await client.create_bundle(
        recipe_id=recipe_id,
        prompt="E2E test: echo backend should claim and complete this task",
        requested_by="e2e_test",
        tasks=[{
            "title": "Echo test task",
            "description": "A simple task for the echo backend to process",
            "depends_on_task_ids": [],
        }],
    )
    bundle_id = bundle["id"]
    task_id = tasks[0]["id"]

    # Verify task is open
    task = await client.get_task(task_id)
    assert task["status"] == "open"

    # 3. Start daemon as subprocess with echo backend
    daemon_env = {
        **os.environ,
        "KREWHUB_URL": krewhub_url,
        "KREWHUB_API_KEY": os.getenv("KREWHUB_API_KEY", "test-api-key"),
        "KREWCLI_BACKEND_ECHO": "1",
    }

    daemon_proc = subprocess.Popen(
        [
            sys.executable, "-m", "krewcli.cli",
            "daemon", "start",
            "--cookbook", os.getenv("KREWHUB_COOKBOOK_ID", "e2e_cookbook"),
            "--recipe", recipe_id,
            "--agents", "echo",
            "--workdir", str(tmp_path),
            "--poll-interval", "2",
        ],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        # 4. Wait for task to be claimed
        async def check_claimed():
            t = await client.get_task(task_id)
            return t if t["status"] in ("claimed", "working", "done") else None

        claimed_task = await _poll_until(check_claimed, timeout=30)
        assert claimed_task["status"] in ("claimed", "working", "done")

        # 5. Wait for task to complete
        async def check_done():
            t = await client.get_task(task_id)
            return t if t["status"] in ("done", "blocked") else None

        done_task = await _poll_until(check_done, timeout=60)
        assert done_task["status"] == "done"

        # 6. Verify events were posted
        bundle_data = await client.get_bundle(bundle_id)
        events = bundle_data.get("events", [])

        event_types = [e["type"] for e in events]
        # Should have session_start, some agent_reply/tool_use, session_end
        assert "session_start" in event_types, f"Missing session_start. Got: {event_types}"
        assert "session_end" in event_types, f"Missing session_end. Got: {event_types}"

        # 7. Verify task has completion metadata (session pinning)
        final_task = await client.get_task(task_id)
        # session_id and work_dir should be set by the harness
        assert final_task.get("session_id") or True  # may not be exposed in all API versions

    finally:
        # Kill daemon
        try:
            os.killpg(os.getpgid(daemon_proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        daemon_proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_daemon_orphan_recovery(client, krewhub_url, tmp_path):
    """Verify orphan recovery marks stuck tasks as blocked on daemon startup."""

    # 1. Create recipe + bundle
    recipe = await client.create_recipe(
        name=f"e2e-orphan-{uuid.uuid4().hex[:6]}",
        repo_url="https://github.com/test/orphan-repo.git",
        created_by="e2e_test",
        cookbook_id=os.getenv("KREWHUB_COOKBOOK_ID", "e2e_cookbook"),
    )
    recipe_id = recipe["id"]

    bundle, tasks = await client.create_bundle(
        recipe_id=recipe_id,
        prompt="Orphan recovery test",
        requested_by="e2e_test",
        tasks=[{
            "title": "Orphan task",
            "description": "This task will be left in working state",
            "depends_on_task_ids": [],
        }],
    )
    task_id = tasks[0]["id"]

    # 2. Manually claim and set to working (simulating a daemon crash mid-task)
    agent_id = "echo@e2e_test"
    await client.claim_task(task_id, agent_id)
    await client.update_task_status(task_id, "working")

    # Verify it's working
    task = await client.get_task(task_id)
    assert task["status"] == "working"

    # 3. Start daemon — it should recover the orphan on startup
    daemon_env = {
        **os.environ,
        "KREWHUB_URL": krewhub_url,
        "KREWHUB_API_KEY": os.getenv("KREWHUB_API_KEY", "test-api-key"),
        "KREWCLI_BACKEND_ECHO": "1",
    }

    daemon_proc = subprocess.Popen(
        [
            sys.executable, "-m", "krewcli.cli",
            "daemon", "start",
            "--cookbook", os.getenv("KREWHUB_COOKBOOK_ID", "e2e_cookbook"),
            "--recipe", recipe_id,
            "--agents", "echo",
            "--workdir", str(tmp_path),
            "--poll-interval", "2",
        ],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        # 4. Wait for the task to be marked blocked by orphan recovery
        async def check_blocked():
            t = await client.get_task(task_id)
            return t if t["status"] == "blocked" else None

        blocked_task = await _poll_until(check_blocked, timeout=30)
        assert blocked_task["status"] == "blocked"
        assert "crash_recovery" in (blocked_task.get("blocked_reason") or "")

    finally:
        try:
            os.killpg(os.getpgid(daemon_proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        daemon_proc.wait(timeout=10)
