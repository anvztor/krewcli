"""Bash execution tool for framework agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic_ai import RunContext


@dataclass
class TaskDeps:
    """Dependencies injected into agent tools at runtime."""
    working_dir: str
    repo_url: str = ""
    branch: str = "main"


async def bash_exec(ctx: RunContext[TaskDeps], command: str) -> str:
    """Execute a shell command in the working directory.

    Args:
        command: The shell command to run.

    Returns:
        Combined stdout and stderr output (truncated to 4000 chars).
    """
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=ctx.deps.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except asyncio.TimeoutError:
        return "Error: command timed out after 120 seconds"
    except Exception as exc:
        return f"Error: {exc}"

    output = stdout.decode() + stderr.decode()
    if len(output) > 4000:
        output = output[:4000] + "\n... (truncated)"
    return output or "(no output)"
