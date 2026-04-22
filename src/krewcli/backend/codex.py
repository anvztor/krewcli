"""Codex CLI backend — rollout-driven subprocess wrapper.

Rewritten from ``agents/codex_agent.py`` to implement the Backend
protocol. Codex's stdout is noisy; the source of truth is the
rollout JSONL file under ``$CODEX_HOME/sessions/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)

logger = logging.getLogger(__name__)

# Allowlisted host env vars — everything else excluded to prevent
# stale KREWHUB_* from prior sessions contaminating this run.
_SAFE_HOST_VARS = frozenset({
    "PATH", "HOME", "SHELL", "TERM", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "SSH_AUTH_SOCK", "DISPLAY", "XDG_RUNTIME_DIR",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
})


class CodexBackend:
    """Codex CLI backend with optional rollout watcher."""

    @property
    def name(self) -> str:
        return "codex"

    async def health(self) -> bool:
        return shutil.which("codex") is not None

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
            _run_codex(prompt, working_dir, env, queue, result_future),
            name="codex-backend",
        )

        return BackendSession(messages=queue, result_future=result_future)


async def _run_codex(
    prompt: str,
    working_dir: str,
    extra_env: dict[str, str] | None,
    queue: asyncio.Queue[BackendMessage | None],
    result_future: asyncio.Future[BackendResult],
) -> None:
    """Spawn codex CLI and produce backend messages."""
    safe_env = {k: v for k, v in os.environ.items() if k in _SAFE_HOST_VARS}
    # Drop CODEX_HOME from extra_env — let codex use its global home.
    filtered_extra = {
        k: v for k, v in (extra_env or {}).items() if k != "CODEX_HOME"
    }
    proc_env = {**safe_env, **filtered_extra}

    args = ["codex", "exec", "--skip-git-repo-check", "--full-auto", prompt]

    await queue.put(BackendMessage(
        kind="session_start",
        body="▶ codex",
        payload={"agent_name": "codex", "prompt": prompt, "cwd": working_dir},
    ))

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            start_new_session=True,
        )
    except FileNotFoundError:
        await queue.put(BackendMessage(
            kind="session_end",
            body="■ codex not found",
            payload={"success": False, "blocked_reason": "Codex CLI not found on PATH"},
        ))
        result_future.set_result(BackendResult(
            success=False,
            summary="Codex CLI not found on PATH",
            blocked_reason="Codex CLI not found on PATH",
        ))
        await queue.put(None)
        return

    stdout_bytes, stderr_bytes = await process.communicate()
    returncode = process.returncode or 0
    success = returncode == 0

    stderr_text = stderr_bytes.decode(errors="replace").strip()
    stdout_text = stdout_bytes.decode(errors="replace").strip()
    combined = stdout_text or stderr_text

    # Try to extract summary from latest rollout file.
    summary = await _extract_rollout_summary(
        fallback=combined,
        success=success,
    )

    if combined:
        await queue.put(BackendMessage(
            kind="agent_reply",
            body=combined[:120],
            payload={"text": combined[:4096]},
        ))

    # Collect code refs from git state.
    from krewcli.agents.code_refs import collect_code_refs
    changed_files, code_refs = await collect_code_refs(
        working_dir=working_dir,
        repo_url="",
        branch="",
    )

    await queue.put(BackendMessage(
        kind="session_end",
        body=f"■ {'done' if success else 'error'}",
        payload={"success": success, "exit_code": returncode},
    ))

    result_future.set_result(BackendResult(
        success=success,
        summary=summary,
        full_output=combined,
        files_modified=changed_files,
        code_refs=[cr.model_dump() for cr in code_refs],
        blocked_reason=None if success else (stderr_text or summary),
    ))
    await queue.put(None)


async def _extract_rollout_summary(
    *,
    fallback: str,
    success: bool,
) -> str:
    """Pull final agent message from the latest codex rollout file."""
    codex_home = str(Path.home() / ".codex")
    sessions_dir = Path(codex_home) / "sessions"
    if not sessions_dir.exists():
        return _fallback(fallback, success)

    try:
        candidates = sorted(
            sessions_dir.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return _fallback(fallback, success)

    if not candidates:
        return _fallback(fallback, success)

    rollout_path = candidates[0]
    last_agent_msg = ""
    final_status = ""

    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                outer = item.get("type", "")
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                inner = payload.get("type", "")

                if outer == "event_msg" and inner == "agent_message":
                    msg = str(payload.get("message") or "").strip()
                    if msg:
                        last_agent_msg = msg
                elif outer == "event_msg" and inner == "task_complete":
                    final_status = "task_complete"
                elif outer == "event_msg" and inner == "turn_aborted":
                    final_status = "turn_aborted"
                elif outer == "response_item" and inner == "message":
                    if payload.get("role") == "assistant":
                        text = _extract_msg_text(payload.get("content", []))
                        if text:
                            last_agent_msg = text
    except OSError:
        return _fallback(fallback, success)

    if last_agent_msg:
        return last_agent_msg[:2000]
    if final_status == "turn_aborted":
        return "Codex turn aborted"
    if final_status == "task_complete":
        return "Codex completed successfully"
    return _fallback(fallback, success)


def _fallback(stderr: str, success: bool) -> str:
    stderr = (stderr or "").strip()
    if stderr:
        return stderr[-800:]
    return "Codex completed successfully" if success else "Codex exited without output"


def _extract_msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for part in content:
        if isinstance(part, dict):
            t = part.get("text") or part.get("content")
            if isinstance(t, str):
                pieces.append(t)
    return "\n".join(pieces)
