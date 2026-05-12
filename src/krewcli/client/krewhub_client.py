from __future__ import annotations

import logging
from typing import Any

import httpx

from krewcli.auth.token_store import load_token

logger = logging.getLogger(__name__)


class _RefreshingBearerAuth(httpx.Auth):
    """httpx.Auth that re-reads ~/.krewcli/token from disk on 401.

    Long-running daemons (e.g. `krewcli join`) cache the JWT at startup.
    When the token is rotated on disk, in-flight requests start returning
    401. This auth handler retries once after reloading the token file,
    so a simple `krewcli login` re-auth suffices to recover without
    restarting the daemon.
    """

    requires_response_body = False

    def __init__(self, initial_token: str) -> None:
        self._token = initial_token

    def _auth_header(self) -> str:
        return f"Bearer {self._token}"

    def auth_flow(self, request):  # type: ignore[override]
        request.headers["Authorization"] = self._auth_header()
        response = yield request
        if response.status_code != 401:
            return
        fresh = load_token()
        if not fresh or fresh == self._token:
            return
        logger.warning(
            "krewhub returned 401; reloaded token from disk and retrying once"
        )
        self._token = fresh
        request.headers["Authorization"] = self._auth_header()
        yield request


class KrewHubClient:
    """Async HTTP client for KrewHub API.

    Auth priority:
      1. Bearer JWT (from SIWE login, stored in ~/.krewcli/token)
      2. X-API-Key (legacy, from config)

    Set acting_as_agent_id to include X-Acting-As header for agent-mode ops.

    When using a Bearer JWT, the client automatically re-reads the token
    from disk on 401 and retries once, so long-running daemons recover
    from token rotation without needing a restart.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        jwt_token: str | None = None,
        acting_as_agent_id: int | None = None,
        verify_ssl: bool = True,
    ) -> None:
        headers: dict[str, str] = {}
        auth: httpx.Auth | None = None
        if jwt_token:
            auth = _RefreshingBearerAuth(jwt_token)
        else:
            headers["X-API-Key"] = api_key
        if acting_as_agent_id is not None:
            headers["X-Acting-As"] = f"agent:{acting_as_agent_id}"

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            auth=auth,
            timeout=30.0,
            verify=verify_ssl,
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

    async def get_task_events(
        self,
        task_id: str,
        *,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """Fetch the historical event tape for a task (oldest-first).

        Used by the brain to build conversational context — prior
        agent_reply / human_followup turns are stitched into the
        prompt so re-claims after a follow-up don't start fresh.
        Returns [] on transport errors so callers can fall back to
        the legacy single-shot prompt.
        """
        try:
            resp = await self._client.get(
                f"/api/v1/tasks/{task_id}/events",
                params={"limit": limit},
            )
            resp.raise_for_status()
        except Exception:
            return []
        body = resp.json()
        return body.get("events", []) if isinstance(body, dict) else []

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
                "payload": payload or {},
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

    # ────────────────────────────────────────────────────────────
    # Daemon runtime (Phase 3 M1)
    # ────────────────────────────────────────────────────────────

    async def register_runtime(
        self,
        agent_id: str,
        account_id: str,
        daemon_version: str | None = None,
        provider: str | None = None,
        host_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register this daemon instance. Returns the runtime id."""
        resp = await self._client.post(
            "/api/v1/agents/runtime/register",
            json={
                "agent_id": agent_id,
                "account_id": account_id,
                "daemon_version": daemon_version,
                "provider": provider,
                "host_info": host_info or {},
            },
        )
        resp.raise_for_status()
        return resp.json()["runtime"]

    async def heartbeat_runtime(self, runtime_id: str) -> dict[str, Any]:
        """Heartbeat this daemon so the server marks it online."""
        resp = await self._client.post(
            f"/api/v1/agents/runtime/{runtime_id}/heartbeat",
        )
        resp.raise_for_status()
        return resp.json()["runtime"]

    # ────────────────────────────────────────────────────────────
    # Daemon poll / session management (Managed Agents rewrite)
    # ────────────────────────────────────────────────────────────

    async def poll_claimable_tasks(
        self,
        recipe_id: str,
    ) -> list[dict[str, Any]]:
        """Poll for open tasks whose dependencies are met.

        Returns tasks in ``open`` status from active bundles. The daemon
        filters locally by agent capabilities before attempting to claim.
        """
        bundles = await self.list_bundles(recipe_id)
        claimable: list[dict[str, Any]] = []
        for bundle in bundles:
            if bundle.get("status") not in ("open", "claimed"):
                continue
            detail = await self.get_bundle(bundle["id"])
            done_ids = {
                t["id"]
                for t in detail.get("tasks", [])
                if t.get("status") == "done"
            }
            for task in detail.get("tasks", []):
                if task.get("status") != "open":
                    continue
                deps = set(task.get("depends_on_task_ids") or [])
                if deps <= done_ids:
                    claimable.append({
                        **task,
                        "bundle_id": bundle["id"],
                        "bundle_prompt": bundle.get("prompt", ""),
                        "recipe_id": recipe_id,
                    })
        return claimable

    async def post_task_completion(
        self,
        task_id: str,
        session_id: str,
        work_dir: str,
        artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pin session_id + work_dir on the task row for crash recovery."""
        resp = await self._client.post(
            f"/api/v1/tasks/{task_id}/completion",
            json={
                "session_id": session_id,
                "work_dir": work_dir,
                "artifacts": artifacts or {},
            },
        )
        resp.raise_for_status()
        return resp.json()["task"]

    async def post_task_progress(
        self,
        task_id: str,
        summary: str,
        step: int | None = None,
        total: int | None = None,
    ) -> dict[str, Any]:
        """Report ephemeral progress for a running task."""
        payload: dict[str, Any] = {"summary": summary}
        if step is not None:
            payload["step"] = step
        if total is not None:
            payload["total"] = total
        resp = await self._client.post(
            f"/api/v1/tasks/{task_id}/progress",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def post_task_usage(
        self,
        task_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record LLM token usage for a completed task."""
        payload: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if model is not None:
            payload["model"] = model
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        resp = await self._client.post(
            f"/api/v1/tasks/{task_id}/usage",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def poll_cancel_status(self, task_id: str) -> bool:
        """Check if a task has been cancelled. Returns True if cancelled."""
        resp = await self._client.get(
            f"/api/v1/tasks/{task_id}/cancel-status",
        )
        resp.raise_for_status()
        return resp.json().get("cancelled", False)

    async def add_task_to_bundle(
        self,
        bundle_id: str,
        title: str,
        description: str,
        depends_on_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a single task to an existing bundle."""
        resp = await self._client.post(
            f"/api/v1/bundles/{bundle_id}/tasks",
            json={
                "title": title,
                "description": description,
                "depends_on_task_ids": depends_on_task_ids or [],
            },
        )
        resp.raise_for_status()
        return resp.json()["task"]

    async def get_working_tasks(
        self,
        agent_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Find tasks in 'working' state claimed by given agent IDs.

        Used by orphan recovery to find stuck tasks from a prior crash.
        """
        working: list[dict[str, Any]] = []
        for agent_id in agent_ids:
            resp = await self._client.get(
                "/api/v1/agents",
                params={"agent_id": agent_id},
            )
            if resp.status_code != 200:
                continue
            presence = resp.json().get("agents", [])
            for p in presence:
                tid = p.get("current_task_id")
                if tid:
                    try:
                        task = await self.get_task(tid)
                        if task.get("status") == "working":
                            working.append(task)
                    except Exception:
                        pass
        return working
