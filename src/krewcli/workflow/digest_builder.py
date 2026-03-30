from __future__ import annotations

import logging
import uuid
from typing import Any

from krewcli.agents.models import TaskResult
from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


class DigestBuilder:
    """Collects task results and builds a digest submission."""

    def __init__(
        self,
        client: KrewHubClient,
        agent_id: str,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._results: dict[str, TaskResult] = {}

    def add_result(self, task_id: str, result: TaskResult) -> None:
        self._results[task_id] = result

    async def submit(self, bundle_id: str) -> dict[str, Any] | None:
        if not self._results:
            logger.warning("No task results to submit as digest")
            return None

        summaries = [r.summary for r in self._results.values()]
        combined_summary = " ".join(summaries)

        task_results = [
            {"task_id": tid, "outcome": res.summary}
            for tid, res in self._results.items()
        ]

        all_facts = []
        for res in self._results.values():
            for f in res.facts:
                all_facts.append({
                    "id": f"f_{uuid.uuid4().hex[:8]}",
                    "claim": f.claim,
                    "source_url": f.source_url,
                    "source_title": f.source_title,
                    "captured_by": self._agent_id,
                    "confidence": f.confidence,
                })

        all_code_refs = []
        for res in self._results.values():
            for c in res.code_refs:
                all_code_refs.append(c.model_dump())

        try:
            digest = await self._client.submit_digest(
                bundle_id=bundle_id,
                submitted_by=self._agent_id,
                summary=combined_summary,
                task_results=task_results,
                facts=all_facts,
                code_refs=all_code_refs,
            )
            logger.info("Digest submitted for bundle %s", bundle_id)
            return digest
        except Exception as exc:
            logger.error("Failed to submit digest for %s: %s", bundle_id, exc)
            return None

    def clear(self) -> None:
        self._results.clear()
