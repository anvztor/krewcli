from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Protocol

from krewcli.agents.models import CodeRefResult, TaskResult


@dataclass
class AgentDeps:
    """Dependencies injected into agent tools at runtime."""

    working_dir: str
    repo_url: str
    branch: str


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
        try:
            completed = await _run_command(
                self._command_builder(prompt),
                deps.working_dir,
                timeout=300,
            )
        except FileNotFoundError:
            result = TaskResult(
                summary=f"{self._name} CLI is not installed",
                success=False,
                blocked_reason=f"{self._name} CLI is not installed",
            )
            return AgentRunResult(output=result)
        except asyncio.TimeoutError:
            result = TaskResult(
                summary=f"{self._name} CLI timed out",
                success=False,
                blocked_reason=f"{self._name} CLI timed out",
            )
            return AgentRunResult(output=result)

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
                files_modified=changed_files,
                code_refs=code_refs,
                success=success,
                blocked_reason=blocked_reason,
            )
        )


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
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise

    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(),
        stderr=stderr.decode(),
    )


def _summarize_output(output: str, *, success: bool, name: str) -> str:
    normalized = " ".join(output.split())
    if normalized:
        return normalized[:280]
    if success:
        return f"{name} completed successfully"
    return f"{name} failed without producing output"
