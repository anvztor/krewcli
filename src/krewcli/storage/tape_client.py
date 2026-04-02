from __future__ import annotations

import logging
from typing import Any

import httpx

from krewcli.storage.interface import TapeContext

logger = logging.getLogger(__name__)


class TapeStorageClient:
    """HTTP client for krewhub's tape endpoints.

    CSI implementation: fetches context from the recipe's tape
    and allows agents to write entries back after task execution.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def load_context(self, recipe_id: str) -> TapeContext:
        """Load context entries since the last anchor.

        Returns a TapeContext with a human-readable summary built
        from anchor and entry payloads.
        """
        try:
            resp = await self._client.get(f"/api/v1/tapes/{recipe_id}/context")
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.debug("Failed to load tape context for %s", recipe_id)
            return TapeContext(tape_name=recipe_id, summary="")

        entries = data.get("entries", [])
        summary = _build_summary(entries)

        # Find last anchor ID from the anchors endpoint
        last_anchor_id = None
        try:
            anchor_resp = await self._client.get(f"/api/v1/tapes/{recipe_id}/anchors")
            anchor_resp.raise_for_status()
            anchors = anchor_resp.json().get("anchors", [])
            if anchors:
                last_anchor_id = anchors[-1].get("id")
        except Exception:
            pass

        return TapeContext(
            tape_name=recipe_id,
            summary=summary,
            entries=entries,
            last_anchor_id=last_anchor_id,
        )

    async def append_entry(
        self,
        recipe_id: str,
        kind: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an entry to the recipe's tape."""
        resp = await self._client.post(
            f"/api/v1/tapes/{recipe_id}/entries",
            json={"kind": kind, "payload": payload, "meta": meta or {}},
        )
        resp.raise_for_status()
        return resp.json().get("entry", {})


def _build_summary(entries: list[dict]) -> str:
    """Build a human-readable summary from tape entries."""
    if not entries:
        return ""

    parts: list[str] = []
    for entry in entries:
        kind = entry.get("kind", "")
        payload = entry.get("payload", {})

        if kind == "anchor":
            digest_summary = payload.get("summary", "")
            if digest_summary:
                parts.append(f"[Approved] {digest_summary}")
        elif kind in ("milestone", "fact_added", "code_pushed"):
            body = payload.get("body", "")
            if body:
                parts.append(f"[{kind}] {body[:200]}")
        elif kind == "prompt":
            body = payload.get("body", "")
            if body:
                parts.append(f"[Request] {body[:200]}")

    return "\n".join(parts[-10:])  # Last 10 entries for context window
