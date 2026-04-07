"""Shared types for the hook writer registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HookWiring:
    """The result of running a per-agent hook config writer.

    `settings_file`: absolute path to the file the writer just wrote.
        For agents that load via an explicit `--settings <path>` flag
        (Claude), this gets passed verbatim to the agent runner.

    `extra_args`: command-line args the agent runner should append
        when spawning the agent (e.g. `["--settings", str(path)]`).

    `env`: env vars the agent runner should inject into the spawned
        process so the agent's hook config can find the bridge URL,
        API key, task_id, etc.

    `files_written`: every file the writer wrote, for logging.
    """

    source: str
    settings_file: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    files_written: list[Path] = field(default_factory=list)
    notes: str = ""
