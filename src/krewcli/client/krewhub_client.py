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

    # --- Cookbooks ---

    async def create_cookbook(self, name: str, owner_id: str) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/cookbooks",
            json={"name": name, "owner_id": owner_id},
        )
        resp.raise_for_status()
        data = resp.json()
        cookbook = data["cookbook"]
        cookbook["existed"] = data.get("existed", False)
        cookbook["clone_url"] = data.get("clone_url", "")
        return cookbook

    async def list_cookbooks(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        params = {"owner_id": owner_id} if owner_id else {}
        resp = await self._client.get("/api/v1/cookbooks", params=params)
        resp.raise_for_status()
        return resp.json()["cookbooks"]

    async def get_cookbook(self, cookbook_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/api/v1/cookbooks/{cookbook_id}")
        resp.raise_for_status()
        return resp.json()

    # --- Recipes ---

    async def list_recipes(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/v1/recipes")
        resp.raise_for_status()
        return resp.json()["recipes"]

    async def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/api/v1/recipes/{recipe_id}")
        resp.raise_for_status()
        return resp.json()

    async def create_recipe(
        self, name: str, repo_url: str, created_by: str, cookbook_id: str,
    ) -> dict[str, Any]:
        resp = await self._client.post("/api/v1/recipes", json={
            "name": name,
            "repo_url": repo_url,
            "created_by": created_by,
            "cookbook_id": cookbook_id,
        })
        resp.raise_for_status()
        return resp.json()["recipe"]

    # --- Bundles ---

    async def list_bundles(self, recipe_id: str) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/api/v1/recipes/{recipe_id}/bundles")
        resp.raise_for_status()
        return resp.json()["bundles"]

    async def get_bundle(self, bundle_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/api/v1/bundles/{bundle_id}")
        resp.raise_for_status()
        return resp.json()

    async def create_bundle(
        self,
        recipe_id: str,
        prompt: str,
        requested_by: str,
        tasks: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Create a bundle with pre-defined tasks.

        tasks: list of {"title": str, "description": str, "depends_on_task_ids": [...]}
        Returns (bundle_dict, tasks_list).
        """
        resp = await self._client.post(
            f"/api/v1/recipes/{recipe_id}/bundles",
            json={
                "prompt": prompt,
                "requested_by": requested_by,
                "tasks": tasks,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["bundle"], data["tasks"]

    async def attach_graph(
        self,
        bundle_id: str,
        code: str,
        *,
        created_by: str = "orchestrator",
    ) -> dict[str, Any]:
        """Attach a validated pydantic-graph artifact to an existing bundle.

        POST /api/v1/bundles/{id}/graph. Krewhub validates the code via its
        sandbox, renders mermaid, creates one task per graph node with
        dependencies, and persists code+mermaid on the bundle row. The
        GraphRunnerController then picks the bundle up on its next reconcile.

        Args:
            bundle_id: existing bundle id (must not already have graph_code).
            code: pydantic-graph source from the orchestrator.
            created_by: actor id stamped on the PLAN event.

        Returns:
            {"bundle": {...}, "tasks": [...]} as returned by the route.

        Raises:
            httpx.HTTPStatusError: 404 if bundle missing, 409 if already
                attached, 422 if the sandbox rejects the code.
        """
        resp = await self._client.post(
            f"/api/v1/bundles/{bundle_id}/graph",
            json={"code": code, "created_by": created_by},
        )
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

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a single task by ID."""
        resp = await self._client.get(f"/api/v1/tasks/{task_id}")
        resp.raise_for_status()
        return resp.json()["task"]

    async def get_bundle_events(self, bundle_id: str) -> list[dict[str, Any]]:
        """Fetch events for a bundle (includes task milestone events)."""
        data = await self.get_bundle(bundle_id)
        return data.get("events", [])

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
        payload: dict | None = None,
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
                "payload": payload,
                "facts": facts or [],
                "code_refs": code_refs or [],
            },
        )
        resp.raise_for_status()
        return resp.json()["event"]

    async def post_events_batch(
        self,
        task_id: str,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """POST a batch of events for a single task.

        Each dict should contain: type, actor_id, body, payload (optional),
        facts (optional), code_refs (optional). Sequence numbers are
        assigned server-side. Used by ``KrewhubEventSink`` to stream
        telemetry from local CLI agents with low HTTP overhead.
        """
        if not events:
            return []
        resp = await self._client.post(
            f"/api/v1/tasks/{task_id}/events:batch",
            json={"events": events},
        )
        resp.raise_for_status()
        return resp.json().get("events", [])

    async def post_recipe_event(
        self,
        recipe_id: str,
        event_type: str,
        actor_id: str,
        body: str,
        facts: list[dict] | None = None,
        code_refs: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Post an agent-level event (no bundle/task required)."""
        resp = await self._client.post(
            f"/api/v1/recipes/{recipe_id}/events",
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

    async def list_agents(self, cookbook_id: str | None = None) -> list[dict[str, Any]]:
        """List online agents, optionally filtered by cookbook."""
        params = {"cookbook_id": cookbook_id} if cookbook_id else {}
        resp = await self._client.get("/api/v1/agents", params=params)
        resp.raise_for_status()
        return resp.json()["agents"]

    async def register_agent(
        self,
        agent_id: str,
        cookbook_id: str,
        display_name: str,
        capabilities: list[str],
        max_concurrent_tasks: int = 1,
        endpoint_url: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/agents/register",
            json={
                "agent_id": agent_id,
                "cookbook_id": cookbook_id,
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
        cookbook_id: str,
        display_name: str,
        capabilities: list[str],
        endpoint_url: str | None = None,
        current_task_id: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/agents/heartbeat",
            json={
                "agent_id": agent_id,
                "cookbook_id": cookbook_id,
                "display_name": display_name,
                "capabilities": capabilities,
                "endpoint_url": endpoint_url,
                "current_task_id": current_task_id,
            },
        )
        resp.raise_for_status()
        return resp.json()["presence"]
