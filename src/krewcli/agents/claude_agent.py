from __future__ import annotations

import subprocess
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from krewcli.agents.models import TaskResult


@dataclass
class AgentDeps:
    """Dependencies injected into agent tools at runtime."""
    working_dir: str
    repo_url: str
    branch: str


def create_claude_agent() -> Agent[AgentDeps, TaskResult]:
    """Create a pydantic-ai agent that delegates to Claude CLI."""

    agent = Agent(
        "anthropic:claude-sonnet-4-5",
        deps_type=AgentDeps,
        output_type=TaskResult,
        instructions=(
            "You are a coding agent that delegates work to the Claude Code CLI. "
            "Use the run_claude tool to execute coding tasks. "
            "After completion, summarize the work, list modified files, "
            "and capture any facts or code references."
        ),
    )

    @agent.tool
    async def run_claude(ctx: RunContext[AgentDeps], prompt: str) -> str:
        """Run Claude Code CLI with the given coding task prompt."""
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--allowedTools", "Edit,Write,Bash,Read,Glob,Grep",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=ctx.deps.working_dir,
        )
        if result.returncode != 0:
            return f"Error (exit {result.returncode}):\n{result.stderr}"
        return result.stdout or "(no output)"

    @agent.tool
    async def list_changed_files(ctx: RunContext[AgentDeps]) -> str:
        """List files changed since last commit."""
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            cwd=ctx.deps.working_dir,
        )
        return result.stdout or "(no changes)"

    @agent.tool
    async def get_latest_commit(ctx: RunContext[AgentDeps]) -> str:
        """Get the latest commit SHA and message."""
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H %s"],
            capture_output=True,
            text=True,
            cwd=ctx.deps.working_dir,
        )
        return result.stdout.strip()

    return agent
