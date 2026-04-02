from __future__ import annotations

from typing import Any

import httpx


class KrewHubClient:
    """Async HTTP client for KrewHub API."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # --- Recipes ---

    async def list_recipes(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/v1/recipes")
        resp.raise_for_status()
        return resp.json()["recipes"]

    async def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/api/v1/recipes/{recipe_id}")
        resp.raise_for_status()
        return resp.json()

    # --- Bundles ---

    async def list_bundles(self, recipe_id: str) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/api/v1/recipes/{recipe_id}/bundles")
        resp.raise_for_status()
        return resp.json()["bundles"]

    async def get_bundle(self, bundle_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/api/v1/bundles/{bundle_id}")
        resp.raise_for_status()
        return resp.json()

    async def list_tasks(
        self,
        recipe_id: str,
        bundle_statuses: tuple[str, ...] = ("open", "claimed", "blocked", "cooked"),
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        bundles = await self.list_bundles(recipe_id)

        for bundle in bundles:
            if bundle.get("status") not in bundle_statuses:
                continue
            bundle_detail = await self.get_bundle(bundle["id"])
            for task in bundle_detail.get("tasks", []):
                tasks.append(
                    {
                        **task,
                        "bundle_status": bundle["status"],
                        "bundle_prompt": bundle["prompt"],
                    }
                )

        return tasks

    # --- Tasks ---

    async def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/tasks/{task_id}/claim",
            json={"agent_id": agent_id},
        )
        resp.raise_for_status()
        return resp.json()["task"]

    async def post_event(
        self,
        task_id: str,
        event_type: str,
        actor_id: str,
        body: str,
        facts: list[dict] | None = None,
        code_refs: list[dict] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/tasks/{task_id}/events",
            json={
                "type": event_type,
                "actor_id": actor_id,
                "actor_type": "agent",
                "body": body,
                "facts": facts or [],
                "code_refs": code_refs or [],
            },
        )
        resp.raise_for_status()
        return resp.json()["event"]

    async def update_task_status(
        self, task_id: str, status: str, blocked_reason: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status}
        if blocked_reason:
            payload["blocked_reason"] = blocked_reason
        resp = await self._client.patch(
            f"/api/v1/tasks/{task_id}/status",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["task"]

    # --- Digest ---

    async def submit_digest(
        self,
        bundle_id: str,
        submitted_by: str,
        summary: str,
        task_results: list[dict] | None = None,
        facts: list[dict] | None = None,
        code_refs: list[dict] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/bundles/{bundle_id}/digest",
            json={
                "submitted_by": submitted_by,
                "summary": summary,
                "task_results": task_results or [],
                "facts": facts or [],
                "code_refs": code_refs or [],
            },
        )
        resp.raise_for_status()
        return resp.json()["digest"]

    async def post_decision(
        self,
        bundle_id: str,
        decision: str,
        decided_by: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/bundles/{bundle_id}/decision",
            json={
                "decision": decision,
                "decided_by": decided_by,
                "note": note,
            },
        )
        resp.raise_for_status()
        return resp.json()["digest"]

    # --- Presence ---

    async def register_agent(
        self,
        agent_id: str,
        recipe_id: str,
        display_name: str,
        capabilities: list[str],
        max_concurrent_tasks: int = 1,
        endpoint_url: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/agents/register",
            json={
                "agent_id": agent_id,
                "recipe_id": recipe_id,
                "display_name": display_name,
                "capabilities": capabilities,
                "max_concurrent_tasks": max_concurrent_tasks,
                "endpoint_url": endpoint_url,
            },
        )
        resp.raise_for_status()
        return resp.json()["presence"]

    async def heartbeat(
        self,
        agent_id: str,
        recipe_id: str,
        display_name: str,
        capabilities: list[str],
        current_task_id: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/agents/heartbeat",
            json={
                "agent_id": agent_id,
                "recipe_id": recipe_id,
                "display_name": display_name,
                "capabilities": capabilities,
                "current_task_id": current_task_id,
            },
        )
        resp.raise_for_status()
        return resp.json()["presence"]
