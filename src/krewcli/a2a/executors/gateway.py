"""Gateway executor — A2A executor that spawns CLI agents on demand.

Each agent type (claude, codex, bub) gets its own GatewayExecutor
instance, mounted at a separate A2A subpath. The executor handles two
flavors of inbound request, distinguished by message metadata:

    1. **Task execution** — metadata has ``task_id``. Normal worker path:
       check capacity, spawn the CLI with the user's prompt, return
       "working" immediately, and let the hook pipeline report completion
       via /api/v1/a2a/callback.

    2. **Planning** — metadata has ``bundle_id`` but no ``task_id``.
       This is the "every agent is also a planner" path so krewhub's
       PlannerDispatchController can route empty-bundle planning requests
       to any onboarded worker. The executor runs the CLI with the codegen
       prompt template via SpawnManager.run_codegen, captures the output,
       and POSTs the resulting graph code to /api/v1/bundles/{bundle_id}/graph
       via the injected KrewHubClient.

Single executor, two modes. No separate planner endpoint, no standalone
planner process — every gateway worker is also a planner by default.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from krewcli.a2a.spawn_manager import SpawnManager
from krewcli.agents.registry import AGENT_REGISTRY

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Max characters of generated code to log on success/failure. Enough to
# see the structure + first few steps without flooding logs with 10KB+
# graphs.
_CODE_LOG_PREVIEW = 600


def _code_preview(code: str, limit: int = _CODE_LOG_PREVIEW) -> str:
    """Return a truncated single-string preview of code for logging."""
    if len(code) <= limit:
        return code
    return code[:limit] + f"\n... [truncated, {len(code) - limit} more bytes]"


def _extract_http_detail(exc: httpx.HTTPStatusError) -> str:
    """Pull the `detail` field from a krewhub 4xx JSON response.

    Krewhub's HTTPException responses are shaped as
    ``{"detail": "human-readable reason"}``. Fall back to the raw body
    text (truncated) if the response isn't JSON or lacks `detail`.
    """
    response = exc.response
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except (ValueError, AttributeError):
        pass
    text = (response.text or "").strip()
    return text[:500] if text else f"HTTP {response.status_code}"


class GatewayExecutor(AgentExecutor):
    """A2A executor that spawns CLI agents on demand + handles planning.

    Task execution path (metadata has ``task_id``):
        1. Extracts the task prompt from the A2A message
        2. Reads task_id from metadata (set by krewhub's dispatcher)
        3. Checks capacity
        4. Spawns the CLI agent via SpawnManager
        5. Returns "working" immediately (async execution)

    Planning path (metadata has ``bundle_id``, no ``task_id``):
        1. Extracts the user prompt from the A2A message
        2. Fetches the cookbook's agent pool via KrewHubClient
        3. Runs the CLI with the codegen prompt template via
           SpawnManager.run_codegen (synchronous, captures stdout)
        4. POSTs the graph code to /bundles/{id}/graph via attach_graph
        5. Emits TaskState.completed / failed accordingly
    """

    def __init__(
        self,
        agent_name: str,
        spawn_manager: SpawnManager,
        agent_id: str,
        max_concurrent: int = 1,
        *,
        krewhub_client: "KrewHubClient | None" = None,
        cookbook_id: str = "",
    ) -> None:
        self._agent_name = agent_name
        self._spawn = spawn_manager
        self._agent_id = agent_id
        self._max_concurrent = max_concurrent
        self._krewhub_client = krewhub_client
        self._cookbook_id = cookbook_id

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        # Check capacity
        running = self._spawn.running_count_for(self._agent_name)
        if running >= self._max_concurrent:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.rejected,
                    final=True,
                    message=new_agent_text_message(
                        f"At capacity ({running}/{self._max_concurrent})"
                    ),
                )
            )
            return

        # Extract prompt and task metadata
        prompt = _extract_text(context)
        metadata = _extract_metadata(context)

        # Resolve per-recipe working directory
        recipe_name = metadata.get("recipe_name", "")
        recipe_ctx = self._spawn.resolve_recipe_context(recipe_name) if recipe_name else {}

        if not prompt:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message("No prompt provided"),
                )
            )
            return

        # Planning vs task execution routing. An empty bundle's planning
        # dispatch carries bundle_id but no task_id; normal task dispatch
        # always carries a task_id set by krewhub's dispatcher.
        bundle_id = metadata.get("bundle_id", "")
        inbound_task_id = metadata.get("task_id", "")
        if bundle_id and not inbound_task_id:
            await self._handle_planning_request(
                context, event_queue,
                bundle_id=bundle_id,
                user_prompt=prompt,
                metadata=metadata,
                recipe_ctx=recipe_ctx,
            )
            return

        krewhub_task_id = inbound_task_id or context.task_id

        # Spawn the CLI agent
        started = await self._spawn.spawn(
            agent_name=self._agent_name,
            agent_id=self._agent_id,
            task_id=krewhub_task_id,
            prompt=prompt,
            working_dir=recipe_ctx.get("working_dir"),
            repo_url=recipe_ctx.get("repo_url"),
            branch=recipe_ctx.get("branch"),
            bundle_id=metadata.get("bundle_id", ""),
            recipe_id=metadata.get("recipe_id", ""),
        )

        if started:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.working,
                    final=False,
                    message=new_agent_text_message(
                        f"Spawned {self._agent_name} CLI for task {krewhub_task_id}"
                    ),
                )
            )
        else:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        f"Failed to spawn {self._agent_name}"
                    ),
                )
            )

    async def _handle_planning_request(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        *,
        bundle_id: str,
        user_prompt: str,
        metadata: dict,
        recipe_ctx: dict,
    ) -> None:
        """Run the codegen prompt through the CLI and POST graph code back.

        Called when a PlannerDispatchController A2A request lands here
        with bundle_id metadata. Unlike task execution (which is async
        and callback-driven), planning is synchronous inside this method
        — the CLI runs inline and we POST the result directly before
        returning the A2A response.
        """
        if self._krewhub_client is None:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        "Planning request received but krewhub_client not "
                        "injected into GatewayExecutor"
                    ),
                )
            )
            return

        cookbook_id = metadata.get("cookbook_id", "") or self._cookbook_id

        # Build a human-readable agent summary for the codegen prompt so
        # the LLM knows which task_kinds it can target. Fetching agents
        # from krewhub here keeps the summary in sync with the live pool.
        agents_summary = self._agent_name
        if cookbook_id:
            try:
                agents = await self._krewhub_client.list_agents(cookbook_id)
                display_names = [
                    a.get("display_name", a.get("agent_id", "unknown"))
                    for a in agents
                    if a.get("status") != "offline"
                ]
                if display_names:
                    agents_summary = ", ".join(display_names)
            except Exception as exc:
                logger.warning(
                    "gateway planning: agent discovery failed: %s", exc,
                )

        await event_queue.enqueue_event(
            _status_event(
                context,
                TaskState.working,
                final=False,
                message=new_agent_text_message(
                    f"Generating graph code via {self._agent_name} "
                    f"for bundle {bundle_id}..."
                ),
            )
        )

        try:
            code = await self._spawn.run_codegen(
                self._agent_name,
                user_prompt,
                agents_summary,
                working_dir=recipe_ctx.get("working_dir", "") or "",
                repo_url=recipe_ctx.get("repo_url", "") or "",
                branch=recipe_ctx.get("branch", "") or "main",
            )
        except Exception as exc:
            logger.exception("gateway planning: codegen crashed")
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        f"Codegen crashed: {exc}"
                    ),
                )
            )
            return

        if not code:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        f"{self._agent_name} returned no graph code"
                    ),
                )
            )
            return

        try:
            await self._krewhub_client.attach_graph(
                bundle_id, code, created_by=self._agent_id,
            )
        except httpx.HTTPStatusError as exc:
            detail = _extract_http_detail(exc)
            status = exc.response.status_code
            logger.error(
                "gateway planning: bundle %s rejected by krewhub "
                "(HTTP %d): %s\n--- generated code (%d bytes) ---\n%s\n--- end ---",
                bundle_id, status, detail, len(code), _code_preview(code),
            )
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        f"krewhub rejected graph code (HTTP {status}): {detail}"
                    ),
                )
            )
            return
        except Exception as exc:
            logger.exception(
                "gateway planning: attach_graph crashed for bundle %s\n"
                "--- generated code (%d bytes) ---\n%s\n--- end ---",
                bundle_id, len(code), _code_preview(code),
            )
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        f"krewhub attach_graph failed: {exc}"
                    ),
                )
            )
            return

        logger.info(
            "gateway planning: bundle %s attached by %s (%d bytes)\n"
            "--- preview ---\n%s\n--- end ---",
            bundle_id, self._agent_id, len(code), _code_preview(code),
        )
        await event_queue.enqueue_event(
            _status_event(
                context,
                TaskState.completed,
                final=True,
                message=new_agent_text_message(
                    f"Attached graph to bundle {bundle_id}"
                ),
            )
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        metadata = _extract_metadata(context)
        krewhub_task_id = metadata.get("task_id", context.task_id)
        await self._spawn.cancel(krewhub_task_id)
        await event_queue.enqueue_event(
            _status_event(
                context,
                TaskState.canceled,
                final=True,
                message=new_agent_text_message("Task cancelled."),
            )
        )


def build_gateway_agent_card(
    agent_name: str,
    host: str,
    port: int,
) -> AgentCard:
    """Build an AgentCard for a gateway-managed CLI agent."""
    entry = AGENT_REGISTRY.get(agent_name, {})
    display_name = entry.get("display_name", agent_name)
    capabilities_list = entry.get("capabilities", [])

    base_url = f"http://{host}:{port}/agents/{agent_name}"

    return AgentCard(
        name=f"gateway:{agent_name}",
        description=(
            f"On-demand {display_name} agent. Spawns a CLI subprocess "
            f"for each task. Stateful: reads/writes files, git state."
        ),
        url=base_url,
        version="0.3.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(transport="JSONRPC", url=base_url),
        ],
        skills=[
            AgentSkill(
                id=f"gateway:{agent_name}",
                name=display_name,
                description=f"Execute coding tasks using {display_name} CLI (on-demand spawn)",
                tags=["code", "implement", "fix", "test", "refactor"] + capabilities_list,
                examples=[
                    "Implement a heartbeat endpoint",
                    "Fix failing tests in the auth module",
                ],
            ),
        ],
    )


def _extract_text(context: RequestContext) -> str:
    if context.message and context.message.parts:
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                return part.root.text
            if hasattr(part, "text"):
                return part.text
    return ""


def _extract_metadata(context: RequestContext) -> dict:
    if context.message and hasattr(context.message, "metadata"):
        meta = context.message.metadata
        if isinstance(meta, dict):
            return meta
    return {}


def _status_event(
    context: RequestContext,
    state: TaskState,
    final: bool,
    message=None,
) -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id=context.task_id,
        context_id=context.context_id,
        final=final,
        status=TaskStatus(state=state, message=message),
    )
