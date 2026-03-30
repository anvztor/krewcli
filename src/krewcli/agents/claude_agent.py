from __future__ import annotations

from krewcli.agents.base import LocalCliAgent


def create_claude_agent() -> LocalCliAgent:
    """Create a local Claude Code CLI wrapper."""

    return LocalCliAgent(
        name="Claude",
        command_builder=lambda prompt: [
            "claude",
            "-p",
            prompt,
            "--allowedTools",
            "Edit,Write,Bash,Read,Glob,Grep",
        ],
    )
