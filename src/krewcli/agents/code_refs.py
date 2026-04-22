"""Shared code ref collection and subprocess utilities.

Extracted from agents/base.py to eliminate the 4x duplication of
git-status / rev-parse / CodeRefResult across LocalCliAgent,
ClaudeStreamAgent, and CodexRolloutAgent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from krewcli.agents.models import CodeRefResult

logger = logging.getLogger(__name__)


async def collect_code_refs(
    working_dir: str,
    repo_url: str,
    branch: str,
) -> tuple[list[str], list[CodeRefResult]]:
    """Collect changed files and build code refs from git state.

    Returns (changed_files, code_refs). The code_refs list is empty
    when no files changed or git info is unavailable.
    """
    changed_files = await list_changed_files(working_dir)
    resolved_repo_url = repo_url or await read_git_value(
        ["git", "config", "--get", "remote.origin.url"], working_dir,
    )
    commit_sha = await read_git_value(
        ["git", "rev-parse", "HEAD"], working_dir,
    )

    code_refs: list[CodeRefResult] = []
    if resolved_repo_url and commit_sha and changed_files:
        code_refs.append(
            CodeRefResult(
                repo_url=resolved_repo_url,
                branch=branch,
                commit_sha=commit_sha,
                paths=changed_files,
            )
        )

    return changed_files, code_refs


async def list_changed_files(working_dir: str) -> list[str]:
    """List files changed in the working directory via git status."""
    status_output = await read_git_value(
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


async def read_git_value(
    args: list[str],
    working_dir: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Run a git command and return its stdout, or empty string on failure."""
    try:
        completed = await run_command(args, working_dir)
    except FileNotFoundError:
        return ""

    if completed.returncode != 0:
        return ""

    value = completed.stdout.strip()
    if not value and not allow_empty:
        return ""
    return value


async def run_command(
    args: list[str],
    working_dir: str,
    *,
    timeout: int = 30,
) -> "CommandResult":
    """Run a subprocess and return its result."""
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


class CommandResult:
    """Result from a subprocess execution."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
