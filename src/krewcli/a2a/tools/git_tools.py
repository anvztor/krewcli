"""Git operation tools for framework agents."""

from __future__ import annotations

import asyncio

from pydantic_ai import RunContext
from krewcli.a2a.tools.bash_tool import TaskDeps


async def git_diff(ctx: RunContext[TaskDeps]) -> str:
    """Show the current git diff (staged and unstaged changes).

    Returns:
        Git diff output or error message.
    """
    return await _git(ctx.deps.working_dir, "diff")


async def git_status(ctx: RunContext[TaskDeps]) -> str:
    """Show the current git status (modified/untracked files).

    Returns:
        Git status output or error message.
    """
    return await _git(ctx.deps.working_dir, "status", "--short")


async def _git(working_dir: str, *args: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return "Error: git command timed out"
    except Exception as exc:
        return f"Error: {exc}"

    output = stdout.decode()
    if process.returncode != 0:
        return f"Error (exit {process.returncode}): {stderr.decode()}"
    return output or "(no output)"
