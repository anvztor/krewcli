"""Core backend protocol — the streaming-first agent interface.

Design follows Anthropic's Managed Agents architecture (Brain/Hands/Session
separation) and multica's ``Backend.Execute() → Session(Messages, Result)``
pattern.

The key insight: backends produce *messages* (streaming events) and a
terminal *result*. They know nothing about krewhub, task IDs, or event
sinks. That wiring lives in the daemon harness.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class BackendMessage:
    """A single streaming event from a backend execution.

    ``kind`` matches krewhub EventType literals so the daemon session
    can forward messages without translation.
    """

    kind: str  # session_start, agent_reply, thinking, tool_use, tool_result, session_end
    body: str = ""
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BackendResult:
    """Terminal result from a backend execution."""

    success: bool
    summary: str
    full_output: str = ""
    files_modified: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    code_refs: list[dict] = field(default_factory=list)
    blocked_reason: str | None = None
    usage: dict | None = None  # {input_tokens, output_tokens, model, cost_usd, duration_ms}


class BackendSession:
    """Streaming-first execution session.

    Consumers iterate ``messages_iter()`` for live streaming events,
    then ``await result()`` for the terminal outcome. The message queue
    uses ``None`` as a sentinel to signal completion.
    """

    def __init__(
        self,
        messages: asyncio.Queue[BackendMessage | None],
        result_future: asyncio.Future[BackendResult],
    ) -> None:
        self._messages = messages
        self._result_future = result_future

    async def messages_iter(self) -> AsyncIterator[BackendMessage]:
        """Yield messages until the backend signals completion."""
        while True:
            msg = await self._messages.get()
            if msg is None:
                return
            yield msg

    async def result(self) -> BackendResult:
        """Await the terminal result (blocks until execution finishes)."""
        return await self._result_future


@runtime_checkable
class Backend(Protocol):
    """Unified agent backend interface.

    Each implementation wraps a local CLI tool and streams its output
    as ``BackendMessage`` events through a ``BackendSession``.
    """

    @property
    def name(self) -> str: ...

    async def execute(
        self,
        prompt: str,
        working_dir: str,
        *,
        env: dict[str, str] | None = None,
    ) -> BackendSession: ...

    async def health(self) -> bool: ...
