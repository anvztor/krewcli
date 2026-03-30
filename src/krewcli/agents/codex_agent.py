from __future__ import annotations

from krewcli.agents.base import LocalCliAgent


def create_codex_agent() -> LocalCliAgent:
    """Create a local Codex CLI wrapper."""

    return LocalCliAgent(
        name="Codex",
        command_builder=lambda prompt: [
            "codex",
            "--quiet",
            "--approval-mode",
            "full-auto",
            prompt,
        ],
    )
