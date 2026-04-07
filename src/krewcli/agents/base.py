from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Protocol

from krewcli.agents.models import CodeRefResult, TaskResult

if TYPE_CHECKING:
    from krewcli.agents.event_sink import EventSink

logger = logging.getLogger(__name__)

# Limit the amount of text per AGENT_REPLY event so a chatty CLI
# doesn't blow up individual rows. Whole-line chunks are preferred;
# overlong lines are soft-wrapped at this width.
_MAX_LINE_CHARS = 2048
# Default timeout applied when AgentDeps.harness is not set.
_DEFAULT_LOCAL_TIMEOUT = 900


@dataclass
class HarnessConfig:
    """Runtime constraints for an agent harness."""

    timeout: int = 300
    max_retries: int = 0
    allowed_tools: tuple[str, ...] = ()


@dataclass
class AgentDeps:
    """Dependencies injected into agent tools at runtime."""

    working_dir: str
    repo_url: str
    branch: str
    system_prompt: str = ""
    harness: HarnessConfig | None = None
    hooks: dict[str, str] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)
    event_sink: "EventSink | None" = None


@dataclass
class AgentRunResult:
    output: TaskResult


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class AgentRunner(Protocol):
    async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult: ...


class LocalCliAgent:
    def __init__(
        self,
        name: str,
        command_builder: Callable[[str], list[str]],
    ) -> None:
        self._name = name
        self._command_builder = command_builder

    async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult:
        """Spawn the CLI and stream its stdout/stderr as AGENT_REPLY events.

        Emits SESSION_START before spawn, a batched AGENT_REPLY event per
        line (or soft-wrapped chunk) on stdout/stderr, and SESSION_END on
        exit. The final TaskResult still carries the full concatenated
        output so the legacy MILESTONE path drives task DONE/BLOCKED
        transitions exactly as before — streaming is purely additive.
        """
        args = self._command_builder(prompt)
        sink = deps.event_sink
        timeout = (
            deps.harness.timeout if deps.harness and deps.harness.timeout
            else _DEFAULT_LOCAL_TIMEOUT
        )

        # Keep the legacy execution path when no sink is attached so callers
        # and tests can still mock command execution without spawning a real CLI.
        if sink is None:
            try:
                completed = await _run_command(
                    args,
                    deps.working_dir,
                    timeout=timeout,
                )
            except FileNotFoundError:
                msg = f"{self._name} CLI is not installed"
                return AgentRunResult(
                    output=TaskResult(
                        summary=msg,
                        success=False,
                        blocked_reason=msg,
                    )
                )
            except asyncio.TimeoutError:
                msg = f"{self._name} CLI timed out after {timeout}s"
                return AgentRunResult(
                    output=TaskResult(
                        summary=msg,
                        success=False,
                        blocked_reason=msg,
                    )
                )

            combined_output = (completed.stdout or completed.stderr or "").strip()
            changed_files = await _list_changed_files(deps.working_dir)
            repo_url = deps.repo_url or await _read_git_value(
                ["git", "config", "--get", "remote.origin.url"], deps.working_dir
            )
            commit_sha = await _read_git_value(["git", "rev-parse", "HEAD"], deps.working_dir)

            code_refs = []
            if repo_url and commit_sha and changed_files:
                code_refs.append(
                    CodeRefResult(
                        repo_url=repo_url,
                        branch=deps.branch,
                        commit_sha=commit_sha,
                        paths=changed_files,
                    )
                )

            success = completed.returncode == 0
            summary = _summarize_output(combined_output, success=success, name=self._name)
            blocked_reason = None if success else summary

            return AgentRunResult(
                output=TaskResult(
                    summary=summary,
                    full_output=combined_output,
                    files_modified=changed_files,
                    code_refs=code_refs,
                    success=success,
                    blocked_reason=blocked_reason,
                )
            )

        # Emit SESSION_START up-front so the UI shows "agent working"
        # even if the subprocess takes a while to print its first line.
        await sink.emit(
            "session_start",
            payload={
                "agent_name": self._name.lower(),
                "cmdline": args,
                "cwd": deps.working_dir,
                "prompt": prompt,
            },
            body=f"▶ {self._name}",
        )

        loop = asyncio.get_running_loop()
        started = loop.time()

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=deps.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            msg = f"{self._name} CLI is not installed"
            await sink.emit(
                "session_end",
                payload={"success": False, "blocked_reason": msg},
                body=f"■ {self._name} not installed",
            )
            return AgentRunResult(
                output=TaskResult(
                    summary=msg, success=False, blocked_reason=msg,
                )
            )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        stdout_task = asyncio.create_task(
            _drain_stream(
                process.stdout, stdout_chunks,
                sink=sink, stream_name="stdout",
            )
        )
        stderr_task = asyncio.create_task(
            _drain_stream(
                process.stderr, stderr_chunks,
                sink=sink, stream_name="stderr",
            )
        )

        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "%s CLI timed out after %ds, killing subprocess",
                self._name, timeout,
            )
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                if process.returncode is None:
                    process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
        finally:
            # Let drainers finish reading anything still buffered
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                stdout_task.cancel()
                stderr_task.cancel()

        duration_ms = int((loop.time() - started) * 1000)
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        combined_output = (stdout_text or stderr_text or "").strip()

        if timed_out:
            msg = f"{self._name} CLI timed out after {timeout}s"
            await sink.emit(
                "session_end",
                payload={
                    "success": False,
                    "duration_ms": duration_ms,
                    "blocked_reason": "timeout",
                    "exit_code": None,
                    "stdout_tail": stdout_text[-2048:],
                    "stderr_tail": stderr_text[-2048:],
                },
                body=f"■ timeout · {duration_ms}ms",
            )
            return AgentRunResult(
                output=TaskResult(
                    summary=msg,
                    full_output=combined_output,
                    success=False,
                    blocked_reason=msg,
                )
            )

        changed_files = await _list_changed_files(deps.working_dir)
        repo_url = deps.repo_url or await _read_git_value(
            ["git", "config", "--get", "remote.origin.url"], deps.working_dir
        )
        commit_sha = await _read_git_value(["git", "rev-parse", "HEAD"], deps.working_dir)

        code_refs = []
        if repo_url and commit_sha and changed_files:
            code_refs.append(
                CodeRefResult(
                    repo_url=repo_url,
                    branch=deps.branch,
                    commit_sha=commit_sha,
                    paths=changed_files,
                )
            )

        exit_code = process.returncode or 0
        success = exit_code == 0
        summary = _summarize_output(combined_output, success=success, name=self._name)
        blocked_reason = None if success else summary

        await sink.emit(
            "session_end",
            payload={
                "success": success,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "files_modified": changed_files,
                "blocked_reason": blocked_reason,
            },
            body=f"■ {'done' if success else 'error'} · {duration_ms}ms",
        )

        return AgentRunResult(
            output=TaskResult(
                summary=summary,
                full_output=combined_output,
                files_modified=changed_files,
                code_refs=code_refs,
                success=success,
                blocked_reason=blocked_reason,
            )
        )


async def _drain_stream(
    reader: asyncio.StreamReader | None,
    sink_buffer: list[str],
    *,
    sink: "EventSink | None",
    stream_name: str,
) -> None:
    """Read a subprocess stream line-by-line.

    Each non-empty line is:
      1. Appended to ``sink_buffer`` (so ``LocalCliAgent.run`` can
         reconstruct ``full_output`` for the legacy MILESTONE path).
      2. Forwarded to ``sink`` as an ``AGENT_REPLY`` event so cookrew
         can render the CLI's output live.

    Overlong lines are soft-wrapped at ``_MAX_LINE_CHARS`` to keep
    event payloads bounded.
    """
    if reader is None:
        return

    block_index = 0
    while True:
        try:
            raw = await reader.readline()
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            raise
        except Exception:
            logger.exception("Error reading %s from subprocess", stream_name)
            break

        if not raw:
            return

        try:
            line = raw.decode(errors="replace")
        except Exception:
            line = repr(raw)

        sink_buffer.append(line)

        if sink is None:
            continue

        stripped = line.rstrip("\n")
        if not stripped:
            continue

        # Soft-wrap overlong lines into multiple events
        offset = 0
        while offset < len(stripped):
            chunk = stripped[offset : offset + _MAX_LINE_CHARS]
            offset += _MAX_LINE_CHARS
            try:
                await sink.emit(
                    "agent_reply",
                    payload={
                        "text": chunk,
                        "block_index": block_index,
                        "stream": stream_name,
                    },
                    body=chunk[:120],
                )
            except Exception:
                logger.debug(
                    "Failed to emit agent_reply from %s stream", stream_name
                )
            block_index += 1


async def _list_changed_files(working_dir: str) -> list[str]:
    status_output = await _read_git_value(
        ["git", "status", "--short"],
        working_dir,
        allow_empty=True,
    )
    if not status_output:
        return []

    paths: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


async def _read_git_value(
    args: list[str],
    working_dir: str,
    *,
    allow_empty: bool = False,
) -> str:
    try:
        completed = await _run_command(args, working_dir)
    except FileNotFoundError:
        return ""

    if completed.returncode != 0:
        return ""

    value = completed.stdout.strip()
    if allow_empty:
        return value
    return value


async def _run_command(
    args: list[str],
    working_dir: str,
    *,
    timeout: int = 30,
) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=working_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.returncode is None:
                process.kill()

        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
        raise

    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(),
        stderr=stderr.decode(),
    )


def _summarize_output(output: str, *, success: bool, name: str) -> str:
    normalized = " ".join(output.split())
    if normalized:
        return normalized
    if success:
        return f"{name} completed successfully"
    return f"{name} failed without producing output"
