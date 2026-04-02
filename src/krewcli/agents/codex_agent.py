from __future__ import annotations

import os

from krewcli.agents.base import LocalCliAgent


def create_codex_agent() -> LocalCliAgent:
    """Create a local Codex CLI wrapper.

    Uses `codex exec --full-auto` for one-shot execution.
    Inherits OPENAI_API_KEY and CODEX_HOME from environment.
    """

    return LocalCliAgent(
        name="Codex",
        command_builder=lambda prompt: [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--full-auto",
            prompt,
        ],
    )
