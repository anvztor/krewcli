from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TapeContext:
    """Context loaded from a tape for injection into a task execution.

    Contains a summary of prior work and the raw entries for agents
    that want to inspect details.
    """

    tape_name: str
    summary: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    last_anchor_id: int | None = None


class AgentStorageInterface(Protocol):
    """CSI equivalent — abstract interface for agent context storage.

    Agents "mount" a tape before executing a task, loading relevant
    context from previous anchors (approved digests). After execution,
    they can write context back for future agents.
    """

    async def load_context(self, recipe_id: str) -> TapeContext:
        """Load context from the recipe's tape since the last anchor."""
        ...

    async def append_entry(
        self,
        recipe_id: str,
        kind: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an entry to the recipe's tape."""
        ...
