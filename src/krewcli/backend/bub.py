"""Bub CLI backend — simple subprocess wrapper.

Bub is the simplest backend: ``bub run <prompt>`` with stdout
captured as agent_reply events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)

logger = logging.getLogger(__name__)

_STREAM_LIMIT = 4 * 1024 * 1024
_MAX_LINE_CHARS = 2048


class BubBackend:
    """Bub CLI backend."""

    @property
    def name(self) -> str:
        return "bub"

    async def health(self) -> bool:
        return shutil.which("bub") is not None

    async def execute(
        self,
        prompt: str,
        working_dir: str,
        *,
        env: dict[str, str] | None = None,
    ) -> BackendSession:
        queue: asyncio.Queue[BackendMessage | None] = asyncio.Queue(maxsize=512)
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[BackendResult] = loop.create_future()

        asyncio.create_task(
            _run_bub(prompt, working_dir, env, queue, result_future),
            name="bub-backend",
        )

        return BackendSession(messages=queue, result_future=result_future)


async def _run_bub(
    prompt: str,
    working_dir: str,
    extra_env: dict[str, str] | None,
    queue: asyncio.Queue[BackendMessage | None],
    result_future: asyncio.Future[BackendResult],
) -> None:
    """Spawn bub CLI and stream its output."""
    args = ["bub", "run", prompt]
    proc_env = {**os.environ, **(extra_env or {})}
    timeout = 900

    await queue.put(BackendMessage(
        kind="session_start",
        body="▶ bub",
        payload={"agent_name": "bub", "prompt": prompt, "cwd": working_dir},
    ))

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            start_new_session=True,
            limit=_STREAM_LIMIT,
        )
    except FileNotFoundError:
        await queue.put(BackendMessage(
            kind="session_end",
            body="■ bub not found",
            payload={"success": False, "blocked_reason": "bub CLI not found on PATH"},
        ))
        result_future.set_result(BackendResult(
            success=False,
            summary="bub CLI not found on PATH",
            blocked_reason="bub CLI not found on PATH",
        ))
        await queue.put(None)
        return

    stdout_chunks: list[str] = []

    async def drain_stream(reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        block_index = 0
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode(errors="replace")
            stdout_chunks.append(line)
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            offset = 0
            while offset < len(stripped):
                chunk = stripped[offset:offset + _MAX_LINE_CHARS]
                offset += _MAX_LINE_CHARS
                await queue.put(BackendMessage(
                    kind="agent_reply",
                    body=chunk[:120],
                    payload={"text": chunk, "block_index": block_index, "stream": "stdout"},
                ))
                block_index += 1

    timed_out = False
    try:
        drain_task = asyncio.create_task(drain_stream(process.stdout))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                process.kill()
            except ProcessLookupError:
                pass
        finally:
            try:
                await asyncio.wait_for(drain_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                drain_task.cancel()
    except Exception:
        logger.exception("bub backend error")

    combined = "".join(stdout_chunks).strip()
    success = not timed_out and (process.returncode or 0) == 0

    if timed_out:
        summary = f"bub CLI timed out after {timeout}s"
        blocked_reason = summary
    else:
        summary = combined if combined else (
            "bub completed successfully" if success else "bub failed"
        )
        blocked_reason = None if success else summary

    from krewcli.agents.code_refs import collect_code_refs
    changed_files, code_refs = await collect_code_refs(
        working_dir=working_dir, repo_url="", branch="",
    )

    await queue.put(BackendMessage(
        kind="session_end",
        body=f"■ {'done' if success else 'error'}",
        payload={"success": success},
    ))

    result_future.set_result(BackendResult(
        success=success,
        summary=summary,
        full_output=combined,
        files_modified=changed_files,
        code_refs=[cr.model_dump() for cr in code_refs],
        blocked_reason=blocked_reason,
    ))
    await queue.put(None)
