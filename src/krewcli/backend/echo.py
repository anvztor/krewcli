"""Echo backend — test/development backend that simulates agent execution.

Produces session_start, a few agent_reply messages, and session_end
with small delays to simulate real execution. Used for E2E tests
and development without requiring a real Claude/Codex CLI.

Activated when the ``echo`` backend name is requested, or when
``KREWCLI_BACKEND_ECHO=1`` is set.
"""

from __future__ import annotations

import asyncio

from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)


class EchoBackend:
    """Test backend that echoes the prompt as a simulated execution."""

    @property
    def name(self) -> str:
        return "echo"

    async def health(self) -> bool:
        return True

    async def execute(
        self,
        prompt: str,
        working_dir: str,
        *,
        env: dict[str, str] | None = None,
    ) -> BackendSession:
        queue: asyncio.Queue[BackendMessage | None] = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[BackendResult] = loop.create_future()

        asyncio.create_task(
            _run_echo(prompt, working_dir, queue, result_future),
            name="echo-backend",
        )

        return BackendSession(messages=queue, result_future=result_future)


async def _run_echo(
    prompt: str,
    working_dir: str,
    queue: asyncio.Queue[BackendMessage | None],
    result_future: asyncio.Future[BackendResult],
) -> None:
    """Simulate agent execution with deterministic output."""
    await queue.put(BackendMessage(
        kind="session_start",
        body="▶ echo",
        payload={
            "agent_name": "echo",
            "prompt": prompt,
            "cwd": working_dir,
            "session_id": "echo-test-session",
        },
    ))

    await asyncio.sleep(0.1)

    await queue.put(BackendMessage(
        kind="thinking",
        body="thinking…",
        payload={"text": f"Analyzing task: {prompt[:200]}"},
    ))

    await asyncio.sleep(0.1)

    await queue.put(BackendMessage(
        kind="tool_use",
        body="Read(README.md)",
        payload={
            "tool_use_id": "echo-tool-1",
            "tool_name": "Read",
            "input": {"file_path": "README.md"},
        },
    ))

    await asyncio.sleep(0.05)

    await queue.put(BackendMessage(
        kind="tool_result",
        body="→ ok",
        payload={
            "tool_use_id": "echo-tool-1",
            "output": "# Project README\nThis is a test project.",
            "is_error": False,
        },
    ))

    await asyncio.sleep(0.1)

    reply_text = f"Echo completed task: {prompt[:500]}"
    await queue.put(BackendMessage(
        kind="agent_reply",
        body=reply_text[:120],
        payload={"text": reply_text},
    ))

    await asyncio.sleep(0.05)

    await queue.put(BackendMessage(
        kind="session_end",
        body="■ done",
        payload={
            "success": True,
            "duration_ms": 350,
            "result_text": reply_text,
        },
    ))

    result_future.set_result(BackendResult(
        success=True,
        summary=reply_text,
        full_output=reply_text,
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "model": "echo",
            "cost_usd": 0.0,
            "duration_ms": 350,
        },
    ))
    await queue.put(None)
