"""
In-memory fork tape for a single task execution.

Accumulates entries locally during task execution using republic's
InMemoryTapeStore, then serializes them for a single HTTP push
to krewhub on task completion.

See: https://tape.systems — fork-merge strategy.
"""

from __future__ import annotations

from typing import Any

from republic import TapeEntry
from republic.tape import InMemoryTapeStore


class ForkTape:
    """In-memory fork tape for a single task.

    Usage::

        tape = ForkTape("bundle-123", "task-456")
        tape.append("milestone", {"body": "step 1"})
        tape.write_handoff_anchor(summary="Done", code_ref={...})
        entries = tape.to_pushable()   # serialize for HTTP push
    """

    def __init__(self, bundle_id: str, task_id: str) -> None:
        self.bundle_id = bundle_id
        self.task_id = task_id
        self._store = InMemoryTapeStore()
        self._tape_name = f"fork:{bundle_id}/{task_id}"

    def append(
        self,
        kind: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Append an entry to the fork tape."""
        entry = TapeEntry(id=0, kind=kind, payload=payload, meta=meta or {})
        self._store.append(self._tape_name, entry)

    def write_handoff_anchor(
        self,
        summary: str,
        facts: list[dict[str, Any]] | None = None,
        decisions: list[str] | None = None,
        code_ref: dict[str, Any] | None = None,
        next_steps: list[str] | None = None,
    ) -> None:
        """Write a handoff anchor marking task completion."""
        payload: dict[str, Any] = {
            "name": f"handoff:{self.bundle_id}/{self.task_id}",
            "phase": "task_complete",
            "summary": summary,
        }
        if facts:
            payload["facts"] = facts
        if decisions:
            payload["decisions"] = decisions
        if code_ref:
            payload["code_ref"] = code_ref
        if next_steps:
            payload["next_steps"] = next_steps
        entry = TapeEntry(id=0, kind="anchor", payload=payload, meta={})
        self._store.append(self._tape_name, entry)

    def to_pushable(self) -> list[dict[str, Any]]:
        """Serialize fork entries for HTTP push (strips id/date)."""
        entries = self._store.read(self._tape_name) or []
        return [
            {"kind": e.kind, "payload": e.payload, "meta": e.meta}
            for e in entries
        ]

    def __len__(self) -> int:
        entries = self._store.read(self._tape_name)
        return len(entries) if entries else 0
