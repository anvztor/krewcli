"""Per-agent hook config writer registry.

Mirrors vibe-island's per-agent installer modules: one writer per
supported agent. Each writer accepts a workspace dir and produces
a `HookWiring` describing the file it wrote and how the agent
runner should consume it.

To add a new agent: drop a `<name>.py` here exporting a top-level
`write(workspace_dir: str) -> HookWiring` function and register it
in `_REGISTRY` below.
"""

from __future__ import annotations

from typing import Callable

from krewcli.hooks.types import HookWiring
from krewcli.hooks.writers import (
    claude as _claude,
    codex as _codex,
    cursor as _cursor,
    droid as _droid,
    gemini as _gemini,
    opencode as _opencode,
)

Writer = Callable[[str], HookWiring]

_REGISTRY: dict[str, Writer] = {
    "claude": _claude.write,
    "codex": _codex.write,
    "cursor": _cursor.write,
    "droid": _droid.write,
    "gemini": _gemini.write,
    "opencode": _opencode.write,
}


def write_for(agent: str, workspace_dir: str) -> HookWiring | None:
    """Run the writer for the named agent. None if no writer registered."""
    writer = _REGISTRY.get(agent)
    if writer is None:
        return None
    return writer(workspace_dir)


def write_all(workspace_dir: str) -> dict[str, HookWiring]:
    """Run every registered writer once. Used by `krewcli onboard`."""
    out: dict[str, HookWiring] = {}
    for name, writer in _REGISTRY.items():
        try:
            out[name] = writer(workspace_dir)
        except Exception as exc:  # noqa: BLE001 — never block onboard
            out[name] = HookWiring(source=name, notes=f"failed: {exc}")
    return out


def supported_agents() -> list[str]:
    return sorted(_REGISTRY.keys())
