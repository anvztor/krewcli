"""SSE Watcher — subscribes to krewhub watch stream for A2A invocations.

Bridges krewhub A2A gateway → local agent execution:
  1. Subscribe SSE: GET /api/v1/watch?resource_type=a2a_invocation
  2. Filter for invocations targeting our agents
  3. Route to local A2A executor (same as local :9999 gateway)
  4. POST result back: POST /a2a/respond
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


class SSEWatcher:
    """Watch krewhub SSE stream for A2A invocations and task assignments."""

    def __init__(
        self,
        krewhub_url: str,
        jwt_token: str,
        owner: str,
        agent_names: list[str],
        on_invocation: Callable[[dict], Awaitable[dict | None]],
    ):
        self._krewhub_url = krewhub_url
        self._jwt_token = jwt_token
        self._owner = owner
        self._agent_names = set(agent_names)
        self._on_invocation = on_invocation
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_seq = 0  # track last seen seq for reconnect

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("SSE watcher started for agents: %s", self._agent_names)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SSE watcher stopped")

    async def _watch_loop(self) -> None:
        """Persistent SSE connection with auto-reconnect."""
        while self._running:
            try:
                await self._watch_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("SSE connection lost: %s, reconnecting in 3s...", e)
                await asyncio.sleep(3)

    async def _watch_once(self) -> None:
        """Single SSE connection session."""
        # Subscribe to ALL events, filter client-side for a2a_invocation
        # Pass since=latest to skip replay of old events
        url = f"{self._krewhub_url}/api/v1/watch?since={self._last_seq}"
        headers = {"Authorization": f"Bearer {self._jwt_token}"}

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    logger.error("SSE connect failed: %d", resp.status_code)
                    await asyncio.sleep(5)
                    return

                logger.info("SSE connected to %s", url)
                async for line in resp.aiter_lines():
                    if not self._running:
                        break
                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if not data_str:
                        continue

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Track sequence for reconnect
                    seq = event.get("seq", 0)
                    if seq > self._last_seq:
                        self._last_seq = seq

                    # Filter for a2a_invocation events only
                    if event.get("resource_type") != "a2a_invocation":
                        continue

                    # The watch event wraps payload in "object" field
                    payload = event.get("object", event)
                    await self._handle_event(payload)

    async def _handle_event(self, event: dict) -> None:
        """Process a watch event."""
        resource_type = event.get("resource_type")
        payload = event.get("payload", {})

        if resource_type != "a2a_invocation":
            return

        agent_name = payload.get("agent_name")
        owner = payload.get("owner")

        if owner != self._owner or agent_name not in self._agent_names:
            return

        invocation_id = payload.get("id")
        logger.info("A2A invocation received: %s for %s/%s", invocation_id, owner, agent_name)

        # Process locally
        try:
            result = await self._on_invocation(payload)

            # Post result back to krewhub
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._krewhub_url}/a2a/respond",
                    json={
                        "invocation_id": invocation_id,
                        "result": result,
                    },
                    headers={"Authorization": f"Bearer {self._jwt_token}"},
                )
                if resp.status_code == 200:
                    logger.info("A2A response posted for %s", invocation_id)
                else:
                    logger.error("A2A response failed: %d %s", resp.status_code, resp.text)

        except Exception as e:
            logger.exception("A2A invocation failed: %s", invocation_id)
            # Post error back
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"{self._krewhub_url}/a2a/respond",
                        json={
                            "invocation_id": invocation_id,
                            "error": str(e),
                        },
                        headers={"Authorization": f"Bearer {self._jwt_token}"},
                    )
            except Exception:
                pass


    async def poll_pending(self) -> None:
        """Fallback: poll for pending invocations (if SSE misses events)."""
        async with httpx.AsyncClient(timeout=10) as client:
            for agent_name in self._agent_names:
                try:
                    resp = await client.get(
                        f"{self._krewhub_url}/a2a/{self._owner}/{agent_name}/pending",
                        headers={"Authorization": f"Bearer {self._jwt_token}"},
                    )
                    if resp.status_code != 200:
                        continue

                    for inv in resp.json():
                        await self._handle_event({
                            "resource_type": "a2a_invocation",
                            "payload": {
                                "id": inv["invocation_id"],
                                "owner": self._owner,
                                "agent_name": agent_name,
                                "method": inv["method"],
                                "params": inv["params"],
                                "message": json.dumps(inv["params"]),
                            },
                        })
                except Exception as e:
                    logger.warning("Poll pending failed for %s: %s", agent_name, e)
