"""Event sink abstraction for streaming structured telemetry from
local CLI agent runs.

The producer (e.g. ClaudeStreamAgent) calls ``emit()`` as it parses
its CLI subprocess stream. A ``KrewhubEventSink`` buffers events in
an asyncio queue and flushes them in batches to the krewhub server
via ``POST /tasks/{id}/events:batch``. This lets cookrew render
tool calls, thinking blocks, and assistant replies live as they
happen — without blocking the agent subprocess on every emit.

The design is layered so that tests and offline runs can drop in
``NullEventSink``; back-pressure is handled by dropping the chattiest
event types (``AGENT_REPLY``, ``THINKING``) when the queue is full,
while critical types (``TOOL_USE``, ``TOOL_RESULT``, ``SESSION_*``)
are always kept.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


# --- Event type constants (string literals match krewhub EventType enum) ---

SESSION_START = "session_start"
SESSION_END = "session_end"
AGENT_REPLY = "agent_reply"
THINKING = "thinking"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
MILESTONE = "milestone"

# Events we drop first under back-pressure (low value per event).
_DROPPABLE_TYPES = frozenset({AGENT_REPLY, THINKING})


class EventSink(Protocol):
    """Producer interface implemented by every sink."""

    async def emit(
        self,
        event_type: str,
        *,
        payload: dict | None = None,
        body: str = "",
    ) -> None: ...

    async def flush(self) -> None: ...


class NullEventSink:
    """No-op sink for tests and offline runs."""

    async def emit(
        self,
        event_type: str,
        *,
        payload: dict | None = None,
        body: str = "",
    ) -> None:
        return None

    async def flush(self) -> None:
        return None


class KrewhubEventSink:
    """Async event sink that batches emits and flushes to krewhub.

    Usage::

        sink = KrewhubEventSink(client, task_id, agent_id)
        try:
            await sink.emit("tool_use", payload={...}, body="Bash(ls)")
            ...
        finally:
            await sink.flush()

    The sink spawns a background flush task on first ``emit()`` and
    drains it on ``flush()`` (idempotent). All mutations go through
    an asyncio.Queue so concurrent producers are safe.
    """

    def __init__(
        self,
        client: "KrewHubClient",
        task_id: str,
        agent_id: str,
        *,
        queue_size: int = 256,
        batch_size: int = 8,
        flush_interval: float = 0.25,
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

    @property
    def dropped_count(self) -> int:
        return self._dropped

    async def emit(
        self,
        event_type: str,
        *,
        payload: dict | None = None,
        body: str = "",
    ) -> None:
        if self._closed:
            return

        event = {
            "type": event_type,
            "actor_id": self._agent_id,
            "actor_type": "agent",
            "body": body[:256],
            "payload": payload,
        }

        if self._flusher is None:
            self._flusher = asyncio.create_task(
                self._flush_loop(), name=f"event_flush:{self._task_id}"
            )

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if event_type in _DROPPABLE_TYPES:
                self._dropped += 1
                return
            # Critical event — block briefly for space, then give up.
            try:
                await asyncio.wait_for(self._queue.put(event), timeout=1.0)
            except asyncio.TimeoutError:
                self._dropped += 1
                logger.warning(
                    "KrewhubEventSink: dropped critical event %s for task %s",
                    event_type, self._task_id,
                )

    async def flush(self) -> None:
        """Drain the queue and stop the background flusher.

        If any events were dropped under back-pressure, a final
        ``event_sink_telemetry`` event is posted so downstream
        observers (digest, UI) can surface data-loss warnings.
        """
        if self._closed:
            return
        self._closed = True

        if self._flusher is not None:
            # wait for the loop to finish draining
            await self._queue.join()
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):
                pass
            self._flusher = None

        # Final sweep in case anything slipped in after the loop exited
        batch: list[dict] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        # If events were dropped, append a telemetry event to the final batch
        if self._dropped > 0:
            batch.append({
                "type": MILESTONE,  # use existing event type
                "actor_id": self._agent_id,
                "actor_type": "agent",
                "body": f"⚠ Event sink dropped {self._dropped} event(s) under back-pressure",
                "payload": {
                    "_telemetry": "event_sink",
                    "dropped_count": self._dropped,
                    "batch_size": self._batch_size,
                    "queue_size": self._queue.maxsize,
                },
            })

        if batch:
            await self._post_batch(batch)

    async def _flush_loop(self) -> None:
        """Background task: collect events and POST them in batches."""
        try:
            while True:
                batch: list[dict] = []
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(), timeout=self._flush_interval
                    )
                    batch.append(first)
                except asyncio.TimeoutError:
                    continue

                # Drain more, up to batch_size, without waiting further.
                while len(batch) < self._batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    await self._post_batch(batch)
                except Exception:
                    logger.exception(
                        "KrewhubEventSink: batch POST failed for task %s",
                        self._task_id,
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
                "KrewhubEventSink: failed to flush %d events for task %s",
                len(batch), self._task_id,
            )
