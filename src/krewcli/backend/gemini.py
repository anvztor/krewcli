"""Gemini CLI backend — stream-json subprocess wrapper.

Mirrors the ClaudeBackend pattern: spawn ``gemini -p <prompt>
--output-format stream-json`` and translate each JSON line into a
BackendMessage. The daemon harness routes those messages to krewhub
so the cookrew-beta EventFeed shows the real per-task agent activity
under the gemini agent's tab.

Gemini's stream-json shape differs from Claude's:
  Claude   {type:"system", subtype:"init"}     → session_start
  Gemini   {type:"init"}                       → session_start

  Claude   {type:"assistant", message:{content:[{type:"text",...}]}}
  Gemini   {type:"message", role:"assistant", content:"…"}

  Claude   {type:"result", result:"…"}         → session_end
  Gemini   {type:"end"} or returncode=0       → session_end (synthetic)

The CLI also writes log noise to stdout (Registering notification
handlers, MCP context refresh, hook execution …). We treat any line
that doesn't parse as JSON as harmless log output and drop it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from krewcli.backend._delegate import (
    delegate_wiring_active,
    prepend_delegate_preamble,
    write_gemini_settings,
)
from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)

logger = logging.getLogger(__name__)

_STREAM_LIMIT = 4 * 1024 * 1024
_LINE_TIMEOUT = 600
_MAX_TEXT_CHARS = 8192


class GeminiBackend:
    """Gemini CLI backend using --output-format stream-json --yolo."""

    @property
    def name(self) -> str:
        return "gemini"

    async def health(self) -> bool:
        return shutil.which("gemini") is not None

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
            _run_gemini(prompt, working_dir, env, queue, result_future),
            name="gemini-backend",
        )

        return BackendSession(messages=queue, result_future=result_future)


async def _run_gemini(
    prompt: str,
    working_dir: str,
    extra_env: dict[str, str] | None,
    queue: asyncio.Queue[BackendMessage | None],
    result_future: asyncio.Future[BackendResult],
) -> None:
    """Spawn gemini CLI and stream its output into the queue."""
    proc_env = {**os.environ, **(extra_env or {})}

    # Per-task delegate wiring (Three Hands Protocol). Gemini's project
    # MCP config lives at `<cwd>/.gemini/settings.json`; pair it with
    # `--allowed-mcp-server-names krewcli-bridge` so the CLI surfaces
    # the bridge's tools even with a strict default policy. Gemini lacks
    # a system-prompt flag, so the delegate guidance is prepended to
    # the user prompt with a clear delimiter.
    final_prompt = prompt
    extra_args: list[str] = []
    if delegate_wiring_active(proc_env):
        try:
            write_gemini_settings(
                working_dir,
                krewhub_url=proc_env.get("KREWHUB_URL", ""),
                task_id=proc_env.get("KREWHUB_TASK_ID", ""),
                session_token=proc_env.get("KREWHUB_SESSION_TOKEN", ""),
                parent_tape_id=proc_env.get("KREWHUB_PARENT_TAPE_ID", ""),
                bundle_id=proc_env.get("KREWHUB_BUNDLE_ID", ""),
                cookbook_id=proc_env.get("KREWHUB_COOKBOOK_ID", ""),
                sandbox_id=proc_env.get("KREWHUB_SANDBOX_ID", ""),
            )
            final_prompt = prepend_delegate_preamble(prompt)
            extra_args += ["--allowed-mcp-server-names", "krewcli-bridge"]
        except OSError as exc:
            logger.warning(
                "gemini backend: failed to write .gemini/settings.json: "
                "%s — delegate tool will be unavailable", exc,
            )

    args = [
        "gemini",
        "-p", final_prompt,
        "--output-format", "stream-json",
        "--yolo",
        *extra_args,
    ]

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
            summary="Gemini CLI not found on PATH",
            blocked_reason="Gemini CLI not found on PATH",
        ))
        await queue.put(None)
        return

    output_text = ""
    is_error = False
    error_text = ""
    saw_session_end = False

    try:
        assert process.stdout is not None
        while True:
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=_LINE_TIMEOUT,
            )
            if not line:
                break

            text = line.decode(errors="replace").strip()
            if not text or not text.startswith("{"):
                # Non-JSON log noise from gemini's startup chatter — drop.
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
            if delta.session_end:
                saw_session_end = True

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
            summary="Gemini CLI timed out after 10 minutes",
            blocked_reason="Gemini CLI timed out",
        ))
        await queue.put(None)
        return

    await process.wait()
    success = process.returncode == 0 and not is_error

    # Gemini's stream doesn't always emit a clean end marker; synthesize
    # one here so downstream UIs always see the session close.
    if not saw_session_end:
        await queue.put(BackendMessage(
            kind="session_end",
            body=f"■ {'done' if success else 'error'}",
            payload={
                "success": success,
                "result_text": output_text[:_MAX_TEXT_CHARS],
                "synthesized": True,
            },
        ))

    summary = output_text.strip() if output_text.strip() else (
        "Gemini completed successfully" if success else
        error_text or "Gemini failed"
    )

    from krewcli.agents.code_refs import collect_code_refs
    changed_files, code_refs = await collect_code_refs(
        working_dir=working_dir, repo_url="", branch="",
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
    __slots__ = ("text", "final_result_text", "is_error", "error_text", "session_end")

    def __init__(self) -> None:
        self.text = ""
        self.final_result_text = ""
        self.is_error = False
        self.error_text = ""
        self.session_end = False


async def _dispatch(
    msg: dict,
    queue: asyncio.Queue[BackendMessage | None],
    prompt: str,
) -> _Delta:
    """Parse one gemini stream-json line and push BackendMessages."""
    delta = _Delta()
    msg_type = msg.get("type", "")

    if msg_type == "init":
        await queue.put(BackendMessage(
            kind="session_start",
            body=f"▶ gemini · {msg.get('model', 'unknown')}",
            payload={
                "agent_name": "gemini",
                "model": msg.get("model"),
                "session_id": msg.get("session_id"),
                "prompt": prompt,
            },
        ))
        return delta

    if msg_type == "message":
        role = msg.get("role")
        content = msg.get("content")
        text = _coerce_text(content)
        if role == "assistant" and text:
            delta.text += text
            await queue.put(BackendMessage(
                kind="agent_reply",
                body=_first_line(text, 120),
                payload={"text": text[:_MAX_TEXT_CHARS]},
            ))
        # role=user echoes the prompt — drop, the daemon already
        # surfaces it as session_start.payload.prompt.
        return delta

    if msg_type in ("tool_call", "tool_use"):
        tool = msg.get("name") or msg.get("tool") or "?"
        await queue.put(BackendMessage(
            kind="tool_use",
            body=f"{tool}({_summarize_input(msg.get('input') or msg.get('arguments') or {})})",
            payload={
                "tool_use_id": msg.get("id", ""),
                "tool_name": tool,
                "input": msg.get("input") or msg.get("arguments") or {},
            },
        ))
        return delta

    if msg_type == "tool_result":
        is_err = bool(msg.get("is_error", False))
        output = _coerce_text(msg.get("content") or msg.get("output", ""))
        await queue.put(BackendMessage(
            kind="tool_result",
            body="→ error" if is_err else "→ ok",
            payload={
                "tool_use_id": msg.get("tool_use_id", "") or msg.get("id", ""),
                "output": output[:_MAX_TEXT_CHARS],
                "is_error": is_err,
            },
        ))
        return delta

    if msg_type in ("end", "result", "session_end"):
        result_text = _coerce_text(msg.get("result") or msg.get("content", ""))
        delta.final_result_text = result_text
        delta.session_end = True
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
                "tokens": msg.get("usage"),
                "result_text": result_text[:_MAX_TEXT_CHARS],
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
