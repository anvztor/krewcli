"""Daemon loop — pull-based task execution.

The main daemon loop that replaces gateway/lifecycle.py. Polls krewhub
for open tasks, claims them, and dispatches to the Harness for execution.

Design follows multica's daemon pattern:
  - Pull-based polling (no reverse channel needed)
  - Semaphore-limited concurrency
  - Orphan recovery on startup
  - Heartbeat registration
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import click

from krewcli.backend.protocol import Backend
from krewcli.backend.registry import BACKEND_INFO
from krewcli.daemon.harness import Harness
from krewcli.daemon.session import Session
from krewcli.daemon.execenv import ExecutionEnvironment
from krewcli.daemon.recovery import recover_orphans
from krewcli.gateway.identity import _get_owner_label, _make_agent_id
from krewcli.presence.heartbeat import HeartbeatLoop

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


class DaemonLoop:
    """Pull-based daemon that polls krewhub for tasks and executes them.

    Usage::

        loop = DaemonLoop(
            client=krewhub_client,
            backends={"claude": ClaudeBackend(), "codex": CodexBackend()},
            cookbook_id="my-cookbook",
            recipe_id="my-recipe",
            working_dir="/path/to/repo",
        )
        await loop.run()  # runs until cancelled
    """

    def __init__(
        self,
        client: "KrewHubClient",
        backends: dict[str, Backend],
        cookbook_id: str,
        recipe_id: str,
        working_dir: str,
        repo_url: str = "",
        branch: str = "",
        max_concurrent: int = 1,
        poll_interval: float = 5.0,
        heartbeat_interval: int = 30,
    ) -> None:
        self._client = client
        self._backends = backends
        self._cookbook_id = cookbook_id
        self._recipe_id = recipe_id
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._owner = _get_owner_label()
        self._agent_ids: dict[str, str] = {}
        self._heartbeats: list[HeartbeatLoop] = []
        self._running_tasks: set[str] = set()

    async def run(self) -> None:
        """Main entry point. Runs until cancelled."""
        click.echo(f"  Daemon starting (owner={self._owner})")
        click.echo(f"  Backends: {list(self._backends.keys())}")
        click.echo(f"  Recipe: {self._recipe_id}")
        click.echo(f"  Max concurrent: {self._max_concurrent}")

        # Build agent IDs
        for name in self._backends:
            self._agent_ids[name] = _make_agent_id(name, self._owner)

        # Recover orphaned tasks from prior crash
        recovered = await recover_orphans(
            self._client, list(self._agent_ids.values()),
        )
        if recovered:
            click.echo(f"  Recovered {recovered} orphaned task(s)")

        # Register agents and start heartbeats
        await self._register_and_heartbeat()

        click.echo(f"  Polling for tasks every {self._poll_interval}s...")

        # Main poll loop
        try:
            while True:
                # Only poll if we have capacity
                if len(self._running_tasks) < self._max_concurrent:
                    task = await self._poll_and_claim()
                    if task:
                        asyncio.create_task(
                            self._handle_task(task),
                            name=f"task:{task['id'][:8]}",
                        )
                    else:
                        await asyncio.sleep(self._poll_interval)
                else:
                    await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            click.echo("  Daemon shutting down...")
            # Wait for running tasks to complete (with timeout)
            if self._running_tasks:
                click.echo(f"  Waiting for {len(self._running_tasks)} running task(s)...")
            # Stop heartbeats
            for hb in self._heartbeats:
                await hb.stop()
            raise

    async def _register_and_heartbeat(self) -> None:
        """Register each backend as an agent in krewhub and start heartbeats."""
        for name in self._backends:
            agent_id = self._agent_ids[name]
            info = BACKEND_INFO.get(name, {})
            display_name = info.get("display_name", name)
            capabilities = info.get("capabilities", ["claim"])

            try:
                await self._client.register_agent(
                    agent_id=agent_id,
                    cookbook_id=self._cookbook_id,
                    display_name=display_name,
                    capabilities=capabilities,
                    max_concurrent_tasks=self._max_concurrent,
                )
                click.echo(f"  Registered {display_name} ({agent_id})")
            except Exception:
                logger.warning(
                    "Registration failed for %s, continuing with heartbeat", name,
                )

            hb = HeartbeatLoop(
                client=self._client,
                agent_id=agent_id,
                cookbook_id=self._cookbook_id,
                display_name=display_name,
                capabilities=capabilities,
                interval=self._heartbeat_interval,
            )
            hb.start()
            self._heartbeats.append(hb)

    async def _poll_and_claim(self) -> dict | None:
        """Poll krewhub for a claimable task and attempt to claim it."""
        try:
            tasks = await self._client.poll_claimable_tasks(self._recipe_id)
        except Exception:
            logger.debug("poll: failed to fetch claimable tasks")
            return None

        if not tasks:
            return None

        # Try to claim the first available task using the first available backend
        for task in tasks:
            # Pick a backend — prefer the assigned agent if specified
            assigned = task.get("assigned_agent_id", "")
            backend_name = None
            for name, agent_id in self._agent_ids.items():
                if assigned and assigned == agent_id:
                    backend_name = name
                    break
            if backend_name is None:
                # No specific assignment — use first available
                backend_name = next(iter(self._backends))

            agent_id = self._agent_ids[backend_name]
            try:
                claimed = await self._client.claim_task(task["id"], agent_id)
                return {
                    **claimed,
                    "backend_name": backend_name,
                    "agent_id": agent_id,
                    "bundle_id": task.get("bundle_id", ""),
                    "bundle_prompt": task.get("bundle_prompt", ""),
                    "recipe_id": task.get("recipe_id", self._recipe_id),
                }
            except Exception:
                # Claim failed (race condition, already claimed, etc.)
                logger.debug("poll: failed to claim task %s", task["id"])
                continue

        return None

    async def _handle_task(self, task: dict) -> None:
        """Execute a claimed task through the harness."""
        task_id = task["id"]
        backend_name = task["backend_name"]
        agent_id = task["agent_id"]

        async with self._semaphore:
            self._running_tasks.add(task_id)
            click.echo(f"  → Executing task {task_id[:8]} via {backend_name}")

            try:
                backend = self._backends[backend_name]
                session = Session(self._client, task_id, agent_id)
                execenv = ExecutionEnvironment(
                    base_dir=self._working_dir,
                    task_id=task_id,
                    bundle_id=task.get("bundle_id", ""),
                    repo_url=self._repo_url,
                    branch=self._branch,
                )

                # Build the prompt from task title + description + bundle context
                prompt = _build_prompt(task)

                harness = Harness(self._client)
                result = await harness.execute(
                    backend=backend,
                    session=session,
                    execenv=execenv,
                    prompt=prompt,
                    task_id=task_id,
                    task_title=task.get("title", ""),
                    task_description=task.get("description", ""),
                    recipe_id=task.get("recipe_id", self._recipe_id),
                    bundle_id=task.get("bundle_id", ""),
                )

                status = "done" if result.success else "blocked"
                click.echo(
                    f"  ✓ Task {task_id[:8]} {status}: {result.summary[:80]}"
                )

            except Exception:
                logger.exception("handle_task: failed for task %s", task_id)
                try:
                    await self._client.update_task_status(
                        task_id, "blocked",
                        blocked_reason="Daemon execution error",
                    )
                except Exception:
                    pass
                click.echo(f"  ✗ Task {task_id[:8]} failed with exception")

            finally:
                self._running_tasks.discard(task_id)


def _build_prompt(task: dict) -> str:
    """Build the agent prompt from task metadata."""
    parts: list[str] = []

    title = task.get("title", "")
    if title:
        parts.append(f"# Task: {title}")

    description = task.get("description", "")
    if description:
        parts.append(f"\n{description}")

    bundle_prompt = task.get("bundle_prompt", "")
    if bundle_prompt:
        parts.append(f"\n## Context\n{bundle_prompt}")

    return "\n".join(parts) if parts else "Complete the assigned task."
