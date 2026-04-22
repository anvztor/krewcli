"""Claude Code CLI backend — stream-json subprocess wrapper.

Rewritten from ``agents/claude_agent.py`` to implement the Backend
protocol. The key change: instead of calling ``sink.emit()`` directly,
this backend pushes ``BackendMessage`` events into a queue. The daemon
harness is responsible for routing those messages to krewhub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)

logger = logging.getLogger(__name__)

# asyncio StreamReader buffer — 4 MB to handle large tool results.
_STREAM_LIMIT = 4 * 1024 * 1024
# Per-line read timeout.
_LINE_TIMEOUT = 600


class ClaudeBackend:
    """Claude Code CLI backend using --output-format stream-json."""

    @property
    def name(self) -> str:
        return "claude"

    async def health(self) -> bool:
        return shutil.which("claude") is not None

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
            _run_claude(prompt, working_dir, env, queue, result_future),
            name="claude-backend",
        )

        return BackendSession(messages=queue, result_future=result_future)


async def _run_claude(
    prompt: str,
    working_dir: str,
    extra_env: dict[str, str] | None,
    queue: asyncio.Queue[BackendMessage | None],
    result_future: asyncio.Future[BackendResult],
) -> None:
    """Spawn claude CLI and stream its output into the queue."""
    args = [
        "claude",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "-p", prompt,
    ]

    proc_env = {**os.environ, **(extra_env or {})}

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
        result_future.set_result(BackendResult(
            success=False,
            summary="Claude CLI not found on PATH",
            blocked_reason="Claude CLI not found on PATH",
        ))
        await queue.put(None)
        return

    output_text = ""
    is_error = False
    error_text = ""

    try:
        while True:
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=_LINE_TIMEOUT,
            )
            if not line:
                break

            text = line.decode().strip()
            if not text:
                continue

            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue

            delta = await _dispatch(msg, queue, prompt)
            output_text += delta.text
            if delta.final_result_text:
                output_text = delta.final_result_text
            if delta.is_error:
                is_error = True
                error_text = delta.error_text or error_text

    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await queue.put(BackendMessage(
            kind="session_end",
            body="■ timeout",
            payload={"success": False, "blocked_reason": "timeout"},
        ))
        result_future.set_result(BackendResult(
            success=False,
            summary="Claude CLI timed out after 10 minutes",
            blocked_reason="Claude CLI timed out",
        ))
        await queue.put(None)
        return

    await process.wait()

    success = process.returncode == 0 and not is_error
    summary = output_text if output_text else (
        "Claude completed successfully" if success else
        error_text or "Claude failed"
    )

    # Collect code refs from git state.
    from krewcli.agents.code_refs import collect_code_refs
    changed_files, code_refs = await collect_code_refs(
        working_dir=working_dir,
        repo_url="",  # daemon fills this
        branch="",
    )

    result_future.set_result(BackendResult(
        success=success,
        summary=summary,
        full_output=output_text,
        files_modified=changed_files,
        code_refs=[cr.model_dump() for cr in code_refs],
        blocked_reason=None if success else (error_text or summary),
    ))
    await queue.put(None)


# ── Stream message parsing ────────────────────────────────────────


class _Delta:
    __slots__ = ("text", "final_result_text", "is_error", "error_text")

    def __init__(self) -> None:
        self.text = ""
        self.final_result_text = ""
        self.is_error = False
        self.error_text = ""


async def _dispatch(
    msg: dict,
    queue: asyncio.Queue[BackendMessage | None],
    prompt: str,
) -> _Delta:
    """Parse one stream-json line and push BackendMessages into the queue."""
    delta = _Delta()
    msg_type = msg.get("type", "")

    if msg_type == "system" and msg.get("subtype") == "init":
        await queue.put(BackendMessage(
            kind="session_start",
            body=f"▶ claude · {msg.get('model', 'unknown')}",
            payload={
                "agent_name": "claude",
                "model": msg.get("model"),
                "cwd": msg.get("cwd"),
                "session_id": msg.get("session_id"),
                "tools": msg.get("tools", []),
                "prompt": prompt,
            },
        ))
        return delta

    if msg_type == "assistant":
        message = msg.get("message", {}) or {}
        content = message.get("content", []) or []
        model = message.get("model")
        for i, block in enumerate(content):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if not text:
                    continue
                delta.text += text
                await queue.put(BackendMessage(
                    kind="agent_reply",
                    body=_first_line(text, 120),
                    payload={"text": text, "block_index": i, "model": model},
                ))
            elif btype == "thinking":
                await queue.put(BackendMessage(
                    kind="thinking",
                    body="thinking…",
                    payload={"text": block.get("thinking", "") or ""},
                ))
            elif btype == "tool_use":
                await queue.put(BackendMessage(
                    kind="tool_use",
                    body=f"{block.get('name', '?')}({_summarize_input(block.get('input', {}))})",
                    payload={
                        "tool_use_id": block.get("id", ""),
                        "tool_name": block.get("name", ""),
                        "input": block.get("input", {}),
                    },
                ))
        return delta

    if msg_type == "user":
        message = msg.get("message", {}) or {}
        content = message.get("content", []) or []
        if isinstance(content, str):
            return delta
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                output = _coerce_text(block.get("content", ""))
                is_err = bool(block.get("is_error", False))
                await queue.put(BackendMessage(
                    kind="tool_result",
                    body="→ error" if is_err else "→ ok",
                    payload={
                        "tool_use_id": block.get("tool_use_id", ""),
                        "output": output[:8192],
                        "is_error": is_err,
                    },
                ))
        return delta

    if msg_type == "result":
        result_text = msg.get("result", "") or ""
        delta.final_result_text = result_text
        is_err = bool(msg.get("is_error", False))
        if is_err:
            delta.is_error = True
            delta.error_text = result_text
        await queue.put(BackendMessage(
            kind="session_end",
            body=f"■ {'done' if not is_err else 'error'}",
            payload={
                "success": not is_err,
                "duration_ms": msg.get("duration_ms"),
                "num_turns": msg.get("num_turns"),
                "tokens": msg.get("usage"),
                "cost_usd": msg.get("total_cost_usd"),
                "result_text": result_text[:4096],
            },
        ))
        return delta

    return delta


def _first_line(text: str, limit: int = 120) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def _summarize_input(value) -> str:
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "query", "description"):
            val = value.get(key)
            if isinstance(val, str) and val:
                return _first_line(val, 60)
        for v in value.values():
            if isinstance(v, str) and v:
                return _first_line(v, 60)
    if isinstance(value, str):
        return _first_line(value, 60)
    return ""


def _coerce_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                else:
                    parts.append(json.dumps(item, default=str))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, default=str)
    return str(content) if content is not None else ""
