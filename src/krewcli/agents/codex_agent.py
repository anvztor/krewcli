"""Codex CLI wrapper — vibe-island-style rollout-driven execution.

Unlike the old `LocalCliAgent` wrapper we used to use, this agent
does NOT wrap codex's stdout into a `TaskResult`. Codex's stdout
is noisy, fragile, and caps-out on long runs — which is how we
kept getting `"Codex CLI timed out"` milestones. The source of
truth for a codex session is its **rollout JSONL file** written
under `$CODEX_HOME/sessions/YYYY/MM/DD/`. Vibe-island's
`CodexSessionWatcher` reads it live; we do the same via
`bridge/codex_watcher.py::CodexRolloutWatcher`.

Execution model:
  1. Start `codex exec --skip-git-repo-check --full-auto <prompt>`
     with stdout routed to DEVNULL and stderr captured as a small
     fallback. No asyncio timeout.
  2. Start a `CodexRolloutWatcher` that tails the session files
     for `$CODEX_HOME` and forwards each rollout item as a
     canonical hook event via the bridge forwarder.
  3. Wait for the subprocess to exit.
  4. Stop the watcher (it drains any remaining lines).
  5. Inspect the tail of the watched rollout file to extract the
     final `event_msg.task_complete` / `turn_aborted` / agent
     message so we can build a real `TaskResult.summary` for the
     MILESTONE callback.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import os
from dataclasses import dataclass
from pathlib import Path

from krewcli.agents import base
from krewcli.agents.base import AgentDeps, AgentRunResult
from krewcli.agents.models import CodeRefResult, TaskResult
from krewcli.bridge.codex_watcher import CodexRolloutWatcher

# Vars to inherit from the host process. Everything else (especially
# stale KREWHUB_* from prior gateway sessions) is excluded. Task-scoped
# vars arrive via deps.context and are overlaid on top.
_SAFE_HOST_VARS = frozenset({
    "PATH", "HOME", "SHELL", "TERM", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "SSH_AUTH_SOCK", "DISPLAY", "XDG_RUNTIME_DIR",
    # Auth providers codex may need
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
})


def _safe_host_env() -> dict[str, str]:
    """Return a filtered copy of os.environ with only safe host vars."""
    return {k: v for k, v in os.environ.items() if k in _SAFE_HOST_VARS}


_ROLLOUT_SUMMARY_HINT: ContextVar[Path | None] = ContextVar(
    "codex_rollout_summary_hint",
    default=None,
)


@dataclass
class CodexRolloutAgent:
    """Rollout-driven codex wrapper."""

    name: str = "Codex"

    async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult:
        # Build a clean subprocess env: allowlisted host vars + task
        # context. Previously this was `{**os.environ, **base_env}` which
        # leaked stale KREWHUB_* vars from prior gateway sessions,
        # contaminating unrelated bundles with misrouted hook events.
        #
        # Also deliberately drops CODEX_HOME — we don't want codex to
        # look at our workspace-local `.codex` because it has no
        # auth.json and would 401.
        base_env = {k: v for k, v in (deps.context or {}).items() if k != "CODEX_HOME"}
        env = {**_safe_host_env(), **base_env}

        args = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--full-auto",
            prompt,
        ]

        if not _should_use_rollout_watcher(deps):
            return await _run_via_command_runner(args, deps=deps)

        # Watcher points at the user's real ~/.codex/sessions/ —
        # the same place codex will write the rollout for this spawn.
        codex_home = str(Path.home() / ".codex")

        watcher = CodexRolloutWatcher(
            codex_home=codex_home,
            env=deps.context or {},
            session_id_hint=None,
        )
        await watcher.start()

        process = None
        stderr_bytes = b""
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=deps.working_dir,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError:
                await watcher.stop()
                return AgentRunResult(output=TaskResult(
                    summary="Codex CLI not found on PATH",
                    success=False,
                    blocked_reason="Codex CLI not found on PATH",
                ))

            try:
                _, stderr_bytes = await process.communicate()
            except asyncio.CancelledError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await watcher.stop()
                raise
        finally:
            await watcher.stop()

        returncode = process.returncode if process else -1
        success = returncode == 0

        token = _ROLLOUT_SUMMARY_HINT.set(
            getattr(watcher, "latest_rollout_path", None)
        )
        try:
            summary = await _extract_summary_from_latest_rollout(
                codex_home=codex_home,
                fallback_stderr=stderr_bytes.decode(errors="replace"),
                success=success,
            )
        finally:
            _ROLLOUT_SUMMARY_HINT.reset(token)

        return await _build_result(
            deps=deps,
            summary=summary,
            full_output=summary,
            success=success,
        )


def _should_use_rollout_watcher(deps: AgentDeps) -> bool:
    context = deps.context or {}
    raw = str(context.get("KREWCLI_CODEX_DISABLE_ROLLOUT_WATCHER") or "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return False
    # Any spawn bound to a task gets the watcher — we don't require
    # CODEX_HOME anymore because we intentionally let codex use its
    # global home for auth, and the watcher tails ~/.codex directly.
    return bool(context.get("KREWHUB_TASK_ID"))


async def _run_via_command_runner(
    args: list[str],
    *,
    deps: AgentDeps,
) -> AgentRunResult:
    try:
        completed = await base._run_command(args, deps.working_dir, timeout=1800)
    except FileNotFoundError:
        return AgentRunResult(output=TaskResult(
            summary="Codex CLI not found on PATH",
            success=False,
            blocked_reason="Codex CLI not found on PATH",
        ))
    except asyncio.TimeoutError:
        return AgentRunResult(output=TaskResult(
            summary="Codex CLI timed out",
            success=False,
            blocked_reason="Codex CLI timed out",
        ))

    success = completed.returncode == 0
    combined_output = (completed.stdout or completed.stderr or "").strip()
    summary = base._summarize_output(combined_output, success=success, name="Codex")

    return await _build_result(
        deps=deps,
        summary=summary,
        full_output=combined_output,
        success=success,
    )


async def _build_result(
    *,
    deps: AgentDeps,
    summary: str,
    full_output: str,
    success: bool,
) -> AgentRunResult:
    changed_files = await base._list_changed_files(deps.working_dir)
    repo_url = deps.repo_url or await base._read_git_value(
        ["git", "config", "--get", "remote.origin.url"], deps.working_dir
    )
    commit_sha = await base._read_git_value(
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

    return AgentRunResult(output=TaskResult(
        summary=summary,
        full_output=full_output,
        files_modified=changed_files,
        code_refs=code_refs,
        success=success,
        blocked_reason=None if success else summary,
    ))


async def _extract_summary_from_rollout(
    *,
    rollout_path: Path | None,
    fallback_stderr: str,
    success: bool,
) -> str:
    """Pull the final assistant message / task_complete payload for one rollout.

    Falls back to stderr when the rollout is missing or unreadable.
    """
    if rollout_path is None or not rollout_path.exists():
        return _fallback(fallback_stderr, success)

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
                    role = payload.get("role", "")
                    if role == "assistant":
                        text = _extract_message_text(payload.get("content", []))
                        if text:
                            last_agent_msg = text
    except OSError:
        return _fallback(fallback_stderr, success)

    if last_agent_msg:
        return last_agent_msg[:2000]
    if final_status == "turn_aborted":
        return "Codex turn aborted"
    if final_status == "task_complete":
        return "Codex completed successfully"
    return _fallback(fallback_stderr, success)


async def _extract_summary_from_latest_rollout(
    *,
    codex_home: str,
    fallback_stderr: str,
    success: bool,
) -> str:
    rollout_path = _ROLLOUT_SUMMARY_HINT.get()
    if rollout_path is None:
        rollout_path = _find_latest_rollout_path(codex_home)
    return await _extract_summary_from_rollout(
        rollout_path=rollout_path,
        fallback_stderr=fallback_stderr,
        success=success,
    )


def _find_latest_rollout_path(codex_home: str) -> Path | None:
    sessions = Path(codex_home) / "sessions"
    if not sessions.exists():
        return None

    try:
        candidates = sorted(
            sessions.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    return candidates[0] if candidates else None


def _fallback(stderr: str, success: bool) -> str:
    stderr = (stderr or "").strip()
    if stderr:
        return stderr[-800:]
    return "Codex completed successfully" if success else "Codex exited without output"


def _extract_message_text(content) -> str:
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


def create_codex_agent() -> CodexRolloutAgent:
    return CodexRolloutAgent()
