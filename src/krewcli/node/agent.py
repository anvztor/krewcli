from __future__ import annotations

import asyncio
import logging
from typing import Any

from krewcli.agents.registry import get_agent_info
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.watch.client import WatchClient, WatchEvent
from krewcli.workflow.digest_builder import DigestBuilder
from krewcli.workflow.task_runner import TaskRunner

logger = logging.getLogger(__name__)


class NodeAgent:
    """Kubelet equivalent for krewcli.

    Lifecycle:
    1. Register with krewhub (POST /agents/register)
    2. Start heartbeat loop
    3. Start watch for tasks assigned to this agent
    4. On task assignment: execute via TaskRunner
    5. Report results back to krewhub
    6. On restart: reconcile — check for currently assigned tasks

    The key shift from the old poll model: the TaskSchedulerController
    in krewhub assigns tasks by setting task.assigned_agent_id. The
    NodeAgent watches for these assignments and executes them.

    Backward compat: the existing claim API is still used to confirm
    task assignments. The poll-based _run_task_worker in cli.py
    continues to work for agents that don't use NodeAgent.
    """

    def __init__(
        self,
        client: KrewHubClient,
        agent_name: str,
        agent_id: str,
        recipe_id: str,
        working_dir: str,
        repo_url: str,
        branch: str,
        *,
        heartbeat_interval: int = 15,
        krewhub_url: str = "",
        api_key: str = "",
    ) -> None:
        self._client = client
        self._agent_name = agent_name
        self._agent_id = agent_id
        self._recipe_id = recipe_id
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch

        info = get_agent_info(agent_name)
        self._display_name = info["display_name"]
        self._capabilities = info["capabilities"]

        self._heartbeat = HeartbeatLoop(
            client=client,
            agent_id=agent_id,
            recipe_id=recipe_id,
            display_name=self._display_name,
            capabilities=self._capabilities,
            interval=heartbeat_interval,
        )

        self._runner = TaskRunner(
            client=client,
            heartbeat=self._heartbeat,
            agent_name=agent_name,
            agent_id=agent_id,
            working_dir=working_dir,
            repo_url=repo_url,
            branch=branch,
        )

        self._watch: WatchClient | None = None
        if krewhub_url and api_key:
            self._watch = WatchClient(
                base_url=krewhub_url,
                api_key=api_key,
                resource_type="task",
                recipe_id=recipe_id,
            )

        self._digest_builders: dict[str, DigestBuilder] = {}
        self._running = False

    async def start(self) -> None:
        """Register with krewhub and start watching for assignments."""
        self._running = True

        # Step 1: Register node
        try:
            await self._client.register_agent(
                agent_id=self._agent_id,
                recipe_id=self._recipe_id,
                display_name=self._display_name,
                capabilities=self._capabilities,
            )
            logger.info("NodeAgent registered: %s", self._agent_id)
        except Exception:
            logger.exception("Failed to register agent, continuing with heartbeat only")

        # Step 2: Start heartbeat
        self._heartbeat.start()

        # Step 3: Reconcile — check for already assigned tasks
        await self._reconcile_on_start()

        # Step 4: Start watch stream (if configured)
        if self._watch is not None:
            self._watch.on_event(self._on_watch_event)
            self._watch.start()
            logger.info("NodeAgent watching for task assignments")

    async def stop(self) -> None:
        self._running = False
        if self._watch is not None:
            await self._watch.stop()
        await self._heartbeat.stop()

    async def _reconcile_on_start(self) -> None:
        """Check for tasks currently assigned to this agent.

        Level-triggered recovery: on restart, the NodeAgent checks
        krewhub for any tasks it should be working on.
        """
        try:
            tasks = await self._client.list_tasks(self._recipe_id)
            for task in tasks:
                assigned = task.get("assigned_agent_id")
                status = task.get("status")
                if assigned == self._agent_id and status == "open":
                    logger.info(
                        "NodeAgent: reconciling assigned task %s",
                        task["id"],
                    )
                    asyncio.create_task(self._execute_task(task["id"]))
        except Exception:
            logger.exception("NodeAgent: reconciliation failed")

    async def _on_watch_event(self, event: WatchEvent) -> None:
        """Handle incoming watch events for task assignments."""
        if event.resource_type != "task":
            return
        if event.event_type not in ("ADDED", "MODIFIED"):
            return

        task_obj = event.object
        assigned = task_obj.get("assigned_agent_id")
        status = task_obj.get("status")

        # Only act on tasks assigned to us that are still open
        if assigned != self._agent_id:
            return
        if status != "open":
            return

        # Don't execute if we're already working on something
        if self._heartbeat.current_task_id is not None:
            return

        task_id = event.resource_id
        logger.info("NodeAgent: task %s assigned, executing", task_id)
        asyncio.create_task(self._execute_task(task_id))

    async def _execute_task(self, task_id: str) -> None:
        """Execute an assigned task via the TaskRunner."""
        try:
            result = await self._runner.claim_and_execute(task_id)
            if result is None or not result.success:
                return

            # Handle digest building
            task_data = await self._client.claim_task(task_id, self._agent_id)
            bundle_id = task_data.get("bundle_id", "")
        except Exception:
            # claim_and_execute already claimed, so get bundle_id differently
            logger.debug("Post-execute bundle lookup, fetching task info")
            return

        try:
            await self._maybe_submit_digest(task_id, result)
        except Exception:
            logger.exception("NodeAgent: digest submission failed for %s", task_id)

    async def _maybe_submit_digest(self, task_id: str, result: Any) -> None:
        """Check if all tasks in the bundle are done and submit digest."""
        # Get task info to find bundle
        tasks = await self._client.list_tasks(self._recipe_id)
        task_info = next((t for t in tasks if t["id"] == task_id), None)
        if task_info is None:
            return

        bundle_id = task_info.get("bundle_id", "")
        if not bundle_id:
            return

        builder = self._digest_builders.setdefault(
            bundle_id, DigestBuilder(client=self._client, agent_id=self._agent_id)
        )
        builder.add_result(task_id, result)

        bundle = await self._client.get_bundle(bundle_id)
        bundle_data = bundle.get("bundle", {})
        bundle_tasks = bundle.get("tasks", [])

        if bundle_data.get("status") == "cooked":
            task_ids = [item["id"] for item in bundle_tasks]
            if builder.has_results_for_tasks(task_ids):
                digest = await builder.submit(bundle_id)
                if digest is not None:
                    self._digest_builders.pop(bundle_id, None)
