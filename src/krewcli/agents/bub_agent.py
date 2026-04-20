from __future__ import annotations

from krewcli.agents.local_cli import LocalCliAgent


def create_bub_agent() -> LocalCliAgent:
    """Create a local bub CLI wrapper."""

    return LocalCliAgent(
        name="bub",
        command_builder=lambda prompt: ["bub", "run", prompt],
    )
