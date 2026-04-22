"""Agent registry — display names and capabilities for krewhub registration.

After the Managed Agents rewrite, the actual agent execution is handled
by ``backend/registry.py``. This module retains the display metadata
used by CLI commands and krewhub registration.
"""

from __future__ import annotations

from typing import Any

from krewcli.backend.registry import BACKEND_INFO


# Re-export BACKEND_INFO as AGENT_REGISTRY for backward compatibility.
AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    name: {
        "display_name": info["display_name"],
        "capabilities": info["capabilities"],
    }
    for name, info in BACKEND_INFO.items()
}


def get_agent_info(name: str) -> dict[str, Any]:
    entry = AGENT_REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"Unknown agent: {name}")
    return {
        "display_name": entry["display_name"],
        "capabilities": entry["capabilities"],
    }
