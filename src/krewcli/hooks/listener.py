"""HTTP hook event listener.

Receives hook events from agent CLIs (Claude, Codex) and forwards them
to KrewHub as recipe-level events via the existing event API.

The listener resolves which recipe an event belongs to by checking the
cwd from the hook payload against known recipe repo paths in the cookbook.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Map hook event names to KrewHub EventType values
HOOK_TO_EVENT_TYPE = {
    "pretooluse": "tool_use",
    "posttooluse": "tool_use",
    "stop": "session_end",
    "sessionstart": "session_start",
}


class HookEventRouter:
    """Routes hook events to the correct recipe based on cwd."""

    def __init__(
        self,
        client: KrewHubClient,
        cookbook_id: str,
        agent_id: str,
        default_recipe_id: str = "",
    ) -> None:
        self._client = client
        self._cookbook_id = cookbook_id
        self._agent_id = agent_id
        self._default_recipe_id = default_recipe_id
        # Cache: repo_path -> recipe_id (populated from cookbook data)
        self._path_to_recipe: dict[str, str] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        """Load cookbook recipes and build path -> recipe_id map."""
        if self._loaded:
            return
        try:
            cb_data = await self._client.get_cookbook(self._cookbook_id)
            for recipe in cb_data.get("recipes", []):
                repo_url = recipe.get("repo_url", "")
                recipe_id = recipe["id"]
                if repo_url:
                    self._path_to_recipe[repo_url] = recipe_id
                if not self._default_recipe_id:
                    self._default_recipe_id = recipe_id
        except Exception as exc:
            logger.warning("Failed to load cookbook recipes: %s", exc)
        self._loaded = True

    def _resolve_recipe(self, cwd: str) -> str:
        """Resolve which recipe a cwd belongs to."""
        for path, recipe_id in self._path_to_recipe.items():
            if cwd.startswith(path) or path.startswith(cwd):
                return recipe_id
        return self._default_recipe_id

    async def handle(self, hook_name: str, payload: dict[str, Any]) -> None:
        """Forward a hook event to KrewHub."""
        await self._ensure_loaded()

        cwd = payload.get("cwd", "")
        recipe_id = self._resolve_recipe(cwd)

        if not recipe_id:
            logger.debug("No recipe for cwd=%s, dropping event %s", cwd, hook_name)
            return

        event_type = HOOK_TO_EVENT_TYPE.get(hook_name, "tool_use")
        event_body = _build_event_body(hook_name, payload)

        try:
            await self._client.post_recipe_event(
                recipe_id=recipe_id,
                event_type=event_type,
                actor_id=self._agent_id,
                body=event_body,
            )
            logger.debug("Forwarded hook %s -> %s (recipe=%s)", hook_name, event_type, recipe_id)
        except Exception as exc:
            logger.warning("Failed to forward hook %s: %s", hook_name, exc)

    def invalidate_cache(self) -> None:
        """Force re-fetch of cookbook recipes on next event."""
        self._loaded = False
        self._path_to_recipe.clear()


def create_hook_listener_app(
    client: KrewHubClient,
    cookbook_id: str,
    agent_id: str,
    default_recipe_id: str = "",
) -> Starlette:
    """Create a Starlette app that receives hook events and forwards to KrewHub."""

    router = HookEventRouter(
        client=client,
        cookbook_id=cookbook_id,
        agent_id=agent_id,
        default_recipe_id=default_recipe_id,
    )

    async def handle_hook(request: Request) -> JSONResponse:
        hook_name = request.path_params["hook_name"]
        try:
            body = await request.body()
            payload: dict[str, Any] = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}

        await router.handle(hook_name, payload)
        return JSONResponse({"status": "ok"})

    async def handle_refresh(request: Request) -> JSONResponse:
        """Force re-fetch of cookbook recipes (call after creating a new recipe)."""
        router.invalidate_cache()
        return JSONResponse({"status": "refreshed"})

    routes = [
        Route("/hooks/{hook_name}", handle_hook, methods=["POST"]),
        Route("/refresh", handle_refresh, methods=["POST"]),
    ]

    return Starlette(routes=routes)


def _build_event_body(hook_name: str, payload: dict[str, Any]) -> str:
    """Build a concise event body from the hook payload."""
    tool_name = payload.get("tool_name", "")
    file_path = payload.get("file_path", "")

    if tool_name:
        parts = [tool_name]
        if file_path:
            parts.append(file_path)
        return " ".join(parts)

    return json.dumps(payload, separators=(",", ":"))[:500]
