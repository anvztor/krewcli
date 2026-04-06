from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


async def dispatch_to_agent(
    a2a_client: httpx.AsyncClient,
    endpoint_url: str,
    task_id: str,
    bundle_id: str,
    prompt: str,
    recipe_meta: dict[str, str],
) -> bool:
    """Send a task to an A2A agent endpoint via JSON-RPC message/send.

    Mirrors the pattern from krewhub/controllers/task_dispatch.py:_send_to_gateway.
    Returns True if the agent accepted the task.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": task_id,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
                "metadata": {
                    "task_id": task_id,
                    "bundle_id": bundle_id,
                    **recipe_meta,
                },
            },
        },
    }
    try:
        resp = await a2a_client.post(endpoint_url, json=payload)
        if resp.status_code == 200:
            body = resp.json()
            result = body.get("result", {})
            state = result.get("status", {}).get("state", "")
            if state in ("submitted", "working", "completed"):
                return True
            if result.get("id"):
                return True
        logger.warning(
            "Agent at %s rejected task %s (status=%d)",
            endpoint_url,
            task_id,
            resp.status_code,
        )
        return False
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        logger.warning(
            "Agent at %s unreachable for task %s: %s", endpoint_url, task_id, exc
        )
        return False


async def wait_for_task_completion(
    krewhub_client: KrewHubClient,
    task_id: str,
    *,
    poll_interval: float = 3.0,
    timeout: float = 300.0,
) -> dict:
    """Poll krewhub GET /tasks/{task_id} until status is done or blocked.

    Returns the final task dict. Raises TimeoutError if timeout exceeded.
    """
    elapsed = 0.0
    while elapsed < timeout:
        task = await krewhub_client.get_task(task_id)
        status = task.get("status", "")
        if status in ("done", "blocked", "cancelled"):
            return task
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")


def pick_available_agent(
    agent_endpoints: dict[str, str],
) -> tuple[str, str]:
    """Pick the first available agent. Returns (agent_id, endpoint_url).

    Simple round-robin placeholder. Future: load-balance by capacity.
    """
    for agent_id, endpoint_url in agent_endpoints.items():
        return agent_id, endpoint_url
    raise RuntimeError("No agents available for dispatch")
