from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from krewcli.agents.base import AgentDeps, AgentRunResult
from krewcli.agents.event_sink import (
    AGENT_REPLY,
    SESSION_END,
    SESSION_START,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
)
from krewcli.agents.models import TaskResult

logger = logging.getLogger(__name__)


@dataclass
class ClaudeStreamAgent:
    """Claude Code CLI wrapper using --output-format stream-json.

    Streams structured JSON from stdout and fans each message out to
    a structured ``EventSink`` (when provided via ``deps.event_sink``)
    so downstream consumers (cookrew) can render tool calls, thinking
    blocks, and intermediate assistant replies live as they happen.

    Runs with --permission-mode bypassPermissions for headless execution
    and inherits the host environment (auth from keychain or
    ANTHROPIC_API_KEY).
    """

    name: str = "Claude"

    async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult:
        args = [
            "claude",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
            "-p", prompt,
        ]

        env = {**os.environ}
        sink = deps.event_sink

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=deps.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            return AgentRunResult(output=TaskResult(
                summary="Claude CLI not found on PATH",
                success=False,
                blocked_reason="Claude CLI not found on PATH",
            ))

        output_text = ""
        is_error = False
        error_text = ""
        session_started = False

        try:
            while True:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=600
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

                output_text_delta = await _dispatch_stream_message(
                    msg, sink=sink, prompt=prompt,
                    session_started=session_started,
                )
                if output_text_delta.session_started:
                    session_started = True
                output_text += output_text_delta.text
                if output_text_delta.final_result:
                    # 'result' message replaces accumulated text with the
                    # claude-provided final result (matches legacy behavior)
                    if output_text_delta.final_result_text:
                        output_text = output_text_delta.final_result_text
                if output_text_delta.is_error:
                    is_error = True
                    error_text = output_text_delta.error_text or error_text

        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            if sink is not None:
                await sink.emit(
                    SESSION_END,
                    payload={"success": False, "blocked_reason": "timeout"},
                    body="■ timeout",
                )
            return AgentRunResult(output=TaskResult(
                summary="Claude CLI timed out after 10 minutes",
                success=False,
                blocked_reason="Claude CLI timed out",
            ))

        await process.wait()

        from krewcli.agents.base import _list_changed_files, _read_git_value
        from krewcli.agents.models import CodeRefResult

        changed_files = await _list_changed_files(deps.working_dir)
        repo_url = deps.repo_url or await _read_git_value(
            ["git", "config", "--get", "remote.origin.url"], deps.working_dir
        )
        commit_sha = await _read_git_value(
            ["git", "rev-parse", "HEAD"], deps.working_dir
        )

        code_refs = []
        if repo_url and commit_sha and changed_files:
            code_refs.append(CodeRefResult(
                repo_url=repo_url,
                branch=deps.branch,
                commit_sha=commit_sha,
                paths=changed_files,
            ))

        success = process.returncode == 0 and not is_error
        summary = output_text if output_text else (
            "Claude completed successfully" if success else
            error_text or "Claude failed"
        )

        return AgentRunResult(output=TaskResult(
            summary=summary,
            full_output=output_text,
            files_modified=changed_files,
            code_refs=code_refs,
            success=success,
            blocked_reason=None if success else (error_text or summary),
        ))


@dataclass
class _StreamDelta:
    text: str = ""
    session_started: bool = False
    final_result: bool = False
    final_result_text: str = ""
    is_error: bool = False
    error_text: str = ""


async def _dispatch_stream_message(
    msg: dict,
    *,
    sink,
    prompt: str,
    session_started: bool,
) -> _StreamDelta:
    """Parse one stream-json line and fan it out to the event sink.

    Returns a _StreamDelta carrying accumulated output_text deltas and
    terminal-state flags so the caller can build the final TaskResult.
    """
    delta = _StreamDelta()
    msg_type = msg.get("type", "")

    if msg_type == "system" and msg.get("subtype") == "init":
        if sink is not None and not session_started:
            await sink.emit(
                SESSION_START,
                payload={
                    "agent_name": "claude",
                    "model": msg.get("model"),
                    "cwd": msg.get("cwd"),
                    "session_id": msg.get("session_id"),
                    "tools": msg.get("tools", []),
                    "prompt": prompt,
                },
                body=f"▶ claude · {msg.get('model', 'unknown')}",
            )
        delta.session_started = True
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
                if sink is not None:
                    await sink.emit(
                        AGENT_REPLY,
                        payload={
                            "text": text,
                            "block_index": i,
                            "model": model,
                        },
                        body=_first_line(text, 120),
                    )
            elif btype == "thinking":
                if sink is not None:
                    thinking_text = block.get("thinking", "") or ""
                    await sink.emit(
                        THINKING,
                        payload={"text": thinking_text},
                        body="thinking…",
                    )
            elif btype == "tool_use":
                if sink is not None:
                    await sink.emit(
                        TOOL_USE,
                        payload={
                            "tool_use_id": block.get("id", ""),
                            "tool_name": block.get("name", ""),
                            "input": block.get("input", {}),
                        },
                        body=f"{block.get('name', '?')}({_summarize_input(block.get('input', {}))})",
                    )
        return delta

    if msg_type == "user":
        # claude emits tool_result blocks as synthetic user messages
        message = msg.get("message", {}) or {}
        content = message.get("content", []) or []
        # Some stream versions embed tool_result under content; others as
        # a plain string — handle both.
        if isinstance(content, str):
            return delta
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                output = _coerce_text(block.get("content", ""))
                is_err = bool(block.get("is_error", False))
                if sink is not None:
                    await sink.emit(
                        TOOL_RESULT,
                        payload={
                            "tool_use_id": block.get("tool_use_id", ""),
                            "output": output[:8192],
                            "is_error": is_err,
                        },
                        body=("→ error" if is_err else "→ ok"),
                    )
        return delta

    if msg_type == "result":
        result_text = msg.get("result", "") or ""
        delta.final_result = True
        delta.final_result_text = result_text
        is_err = bool(msg.get("is_error", False))
        if is_err:
            delta.is_error = True
            delta.error_text = result_text
        if sink is not None:
            await sink.emit(
                SESSION_END,
                payload={
                    "success": not is_err,
                    "duration_ms": msg.get("duration_ms"),
                    "num_turns": msg.get("num_turns"),
                    "tokens": msg.get("usage"),
                    "cost_usd": msg.get("total_cost_usd"),
                    "result_text": result_text[:4096],
                },
                body=f"■ {'done' if not is_err else 'error'}",
            )
        return delta

    return delta


def _first_line(text: str, limit: int = 120) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def _summarize_input(value) -> str:
    """Produce a short human-readable preview of a tool's input dict."""
    if isinstance(value, dict):
        # Prefer a 'command' field (Bash), then 'file_path', then any string.
        for key in ("command", "file_path", "path", "query", "description"):
            val = value.get(key)
            if isinstance(val, str) and val:
                return _first_line(val, 60)
        # Fallback: first string value.
        for v in value.values():
            if isinstance(v, str) and v:
                return _first_line(v, 60)
    if isinstance(value, str):
        return _first_line(value, 60)
    return ""


def _coerce_text(content) -> str:
    """Flatten a tool_result content field to plain text."""
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


def create_claude_agent() -> ClaudeStreamAgent:
    """Create a Claude Code CLI wrapper using stream-json output."""
    return ClaudeStreamAgent()
