"""Session — append-only event log bridge to krewhub.

The Session is the core abstraction from Anthropic's Managed Agents
architecture. All execution state is externalized to krewhub's event
log via ``POST /tasks/{id}/events:batch``. The harness is stateless;
if it crashes, a new harness can resume by reading the event log.

This replaces ``agents/event_sink.py:KrewhubEventSink`` with:
  - Session pinning (crash resilience via early session_id + work_dir)
  - Timer-based 500ms batch flush (vs. drain-on-first-item)
  - Usage reporting
  - Cancel polling
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from krewcli.backend.protocol import BackendMessage

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Event types we drop under back-pressure (high volume, low value per event).
_DROPPABLE_TYPES = frozenset({"agent_reply", "thinking"})


class Session:
    """Append-only event log for a single task execution.

    All state lives in krewhub. The harness can crash and resume
    by creating a new Session for the same task_id.
    """

    def __init__(
        self,
        client: "KrewHubClient",
        task_id: str,
        agent_id: str,
        *,
        queue_size: int = 256,
        batch_size: int = 8,
        flush_interval: float = 0.5,
    ) -> None:
        self._client = client
        self._task_id = task_id
        self._agent_id = agent_id
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_size)
        self._flusher: asyncio.Task | None = None
        self._closed = False
        self._dropped = 0
        self._session_token = str(uuid.uuid4())

    @property
    def session_token(self) -> str:
        return self._session_token

    async def pin(self, session_id: str, work_dir: str) -> None:
        """Pin session metadata early for crash resilience.

        Calls ``POST /tasks/{id}/completion`` so that if the daemon
        crashes, recovery knows the session_id + work_dir to resume.
        """
        try:
            await self._client.post_task_completion(
                self._task_id, session_id, work_dir,
            )
        except Exception:
            logger.warning(
                "Session.pin failed for task %s — continuing without pin",
                self._task_id,
            )

    async def append(
        self,
        event_type: str,
        body: str = "",
        payload: dict | None = None,
        *,
        facts: list[dict] | None = None,
        code_refs: list[dict] | None = None,
    ) -> None:
        """Append an event to the session log. Buffered and batch-flushed."""
        if self._closed:
            return

        event: dict = {
            "type": event_type,
            "actor_id": self._agent_id,
            "actor_type": "agent",
            "body": body[:256],
            "payload": payload,
            "session_token": self._session_token,
        }
        if facts:
            event["facts"] = facts
        if code_refs:
            event["code_refs"] = code_refs

        if self._flusher is None:
            self._flusher = asyncio.create_task(
                self._flush_loop(), name=f"session_flush:{self._task_id}",
            )

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if event_type in _DROPPABLE_TYPES:
                self._dropped += 1
                return
            try:
                await asyncio.wait_for(self._queue.put(event), timeout=1.0)
            except asyncio.TimeoutError:
                self._dropped += 1
                logger.warning(
                    "Session: dropped critical event %s for task %s",
                    event_type, self._task_id,
                )

    async def append_from_backend(self, msg: BackendMessage) -> None:
        """Convert a BackendMessage to a krewhub event and append it."""
        await self.append(msg.kind, body=msg.body, payload=msg.payload)

    async def report_usage(self, usage: dict) -> None:
        """Report LLM token usage to krewhub."""
        try:
            await self._client.post_task_usage(
                self._task_id,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                model=usage.get("model"),
                cost_usd=usage.get("cost_usd"),
                duration_ms=usage.get("duration_ms"),
            )
        except Exception:
            logger.warning(
                "Session: failed to report usage for task %s", self._task_id,
            )

    async def check_cancelled(self) -> bool:
        """Poll krewhub to check if the task has been cancelled."""
        try:
            return await self._client.poll_cancel_status(self._task_id)
        except Exception:
            return False

    async def flush(self) -> None:
        """Drain the queue and stop the background flusher."""
        if self._closed:
            return
        self._closed = True

        if self._flusher is not None:
            await self._queue.join()
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):
                pass
            self._flusher = None

        # Final sweep
        batch: list[dict] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        if self._dropped > 0:
            batch.append({
                "type": "milestone",
                "actor_id": self._agent_id,
                "actor_type": "agent",
                "body": f"⚠ Event sink dropped {self._dropped} event(s) under back-pressure",
                "payload": {
                    "_telemetry": "event_sink",
                    "dropped_count": self._dropped,
                },
                "session_token": self._session_token,
            })

        if batch:
            await self._post_batch(batch)

    async def _flush_loop(self) -> None:
        """Background task: accumulate events for flush_interval then POST."""
        try:
            while True:
                batch: list[dict] = []
                # Wait up to flush_interval for first event.
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(), timeout=self._flush_interval,
                    )
                    batch.append(first)
                except asyncio.TimeoutError:
                    continue

                # Drain more up to batch_size without waiting.
                while len(batch) < self._batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    await self._post_batch(batch)
                except Exception:
                    logger.exception(
                        "Session: batch POST failed for task %s", self._task_id,
                    )
                finally:
                    for _ in batch:
                        self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _post_batch(self, batch: list[dict]) -> None:
        if not batch:
            return
        try:
            await self._client.post_events_batch(self._task_id, batch)
        except Exception:
            logger.exception(
                "Session: failed to flush %d events for task %s",
                len(batch), self._task_id,
            )
