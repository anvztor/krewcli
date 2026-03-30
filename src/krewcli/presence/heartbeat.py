from __future__ import annotations

import asyncio
import logging

from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


class HeartbeatLoop:
    """Background heartbeat loop to KrewHub."""

    def __init__(
        self,
        client: KrewHubClient,
        agent_id: str,
        recipe_id: str,
        display_name: str,
        capabilities: list[str],
        interval: int = 15,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._recipe_id = recipe_id
        self._display_name = display_name
        self._capabilities = capabilities
        self._interval = interval
        self._current_task_id: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def current_task_id(self) -> str | None:
        return self._current_task_id

    @current_task_id.setter
    def current_task_id(self, value: str | None) -> None:
        self._current_task_id = value

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._client.heartbeat(
                    agent_id=self._agent_id,
                    recipe_id=self._recipe_id,
                    display_name=self._display_name,
                    capabilities=self._capabilities,
                    current_task_id=self._current_task_id,
                )
                logger.debug("Heartbeat sent (task=%s)", self._current_task_id)
            except Exception as exc:
                logger.warning("Heartbeat failed: %s", exc)
            await asyncio.sleep(self._interval)
