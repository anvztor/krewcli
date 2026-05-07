"""Daemon loop — SSE-watch-driven task execution.

The daemon is the "kubelet" of the krew platform. It watches krewhub
(the "API server") for A2A invocations via the SSEWatcher informer
pattern, then executes tasks through the Harness (ARI).

Delivery model (from krewwatch):
  Primary: poll krewhub /a2a/{owner}/{agent}/pending (reliable)
  Secondary: SSE watch stream for instant delivery (best-effort)
  Both paths dedup via invocation_id.

When an A2A invocation arrives (either from GraphRunnerController
or any external caller), the daemon:
  1. Extracts task_id + prompt from the invocation
  2. Fetches task metadata from krewhub
  3. Runs the task through the Harness (Backend + Session + ExecEnv)
  4. POSTs the result back to /a2a/respond (completing the lifecycle)

Additionally, the daemon:
  - Detects empty bundles and generates graph code (planner)
  - Polls for orphan open tasks as a fallback (pull-based)
  - Recovers orphaned working tasks on startup
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import click

from krewcli.backend.protocol import Backend
from krewcli.backend.registry import BACKEND_INFO
from krewcli.daemon.harness import Harness
from krewcli.daemon.session import Session
from krewcli.daemon.execenv import ExecutionEnvironment
from krewcli.daemon.recovery import recover_orphans
from krewcli.daemon import supervisor
from krewcli.gateway.identity import _get_owner_label, _make_agent_id
from krewcli.presence.heartbeat import HeartbeatLoop, RuntimeHeartbeat

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


def _device_id() -> str:
    """Return a stable per-device id for daemon runtime de-duplication."""
    path = Path.home() / ".krewcli" / "device-id"
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = f"dev_{uuid4().hex[:16]}"
        path.write_text(value, encoding="utf-8")
        os.chmod(path, 0o600)
        return value
    except OSError:
        return f"host_{socket.gethostname() or platform.node() or 'unknown'}"


def _host_info(endpoint_url: str) -> dict[str, object]:
    return {
        "device_id": _device_id(),
        "hostname": socket.gethostname() or platform.node(),
        "platform": platform.platform(),
        "pid": os.getpid(),
        "runtime": "krewcli-daemon",
        "endpoint_url": endpoint_url,
    }


class DaemonLoop:
    """SSE-watch-driven daemon that receives A2A invocations and executes tasks.

    Usage::

        loop = DaemonLoop(
            client=krewhub_client,
            backends={"claude": ClaudeBackend()},
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
        self._runtime_ids: dict[str, str] = {}
        self._heartbeats: list[HeartbeatLoop] = []
        self._runtime_heartbeats: list[RuntimeHeartbeat] = []
        self._running_tasks: set[str] = set()
        self._task_jobs: set[asyncio.Task] = set()
        self._planning_bundle: str | None = None
        self._watcher = None

    async def run(self) -> None:
        """Main entry point. Runs until cancelled."""
        from krewwatch import SSEWatcher
        from krewcli.auth.token_store import load_token

        click.echo(f"  Daemon starting (owner={self._owner})")
        click.echo(f"  Backends: {list(self._backends.keys())}")
        click.echo(f"  Recipe: {self._recipe_id}")
        click.echo(f"  Max concurrent: {self._max_concurrent}")

        # Track A1: krewhub requires a Bearer JWT minted by krewauth.
        # Surface a clear hint up front when no token is on disk.
        if load_token() is None:
            click.echo(
                "  WARNING: no krewauth token found at ~/.krewcli/token. "
                "Run `krewcli login` first; krewhub will reject all "
                "unauthenticated calls with 401.",
                err=True,
            )

        # Build agent IDs
        for name in self._backends:
            self._agent_ids[name] = _make_agent_id(name, self._owner)

        # Recover orphaned tasks from prior crash
        recovered = await recover_orphans(
            self._client, list(self._agent_ids.values()),
        )
        if recovered:
            click.echo(f"  Recovered {recovered} orphaned task(s)")

        # Register agents (with endpoint_url for A2A hub routing)
        await self._register_and_heartbeat()

        # Mark the daemon ready in the status sidecar so the supervisor
        # can confirm bootstrap completion and `daemon status` can report
        # registered agents/cookbook/recipe without scraping logs.
        supervisor.update_status({
            "ready": True,
            "agents": list(self._agent_ids.keys()),
            "agent_ids": dict(self._agent_ids),
            "runtime_ids": dict(self._runtime_ids),
            "registered_at": int(asyncio.get_event_loop().time()),
        })

        # Start SSEWatcher — the informer that watches krewhub for
        # A2A invocations via dual-path delivery (poll + SSE).
        jwt_token = load_token() or ""
        self._watcher = SSEWatcher(
            krewhub_url=self._client._client.base_url.__str__().rstrip("/"),
            jwt_token=jwt_token,
            owner=self._owner,
            agent_names=list(self._agent_ids.keys()),
            on_invocation=self._handle_invocation,
            poll_interval=self._poll_interval,
            token_reloader=load_token,
            # Match the daemon's harness semaphore so the watcher
            # doesn't queue invocations the harness can't run anyway.
            max_concurrent_invocations=self._max_concurrent,
        )
        self._watcher.start()
        click.echo(f"  SSE watcher started (poll={self._poll_interval}s)")

        # Poll pending once at startup to catch anything missed
        try:
            await self._watcher.poll_pending()
        except Exception:
            logger.debug("startup poll_pending failed")

        click.echo("  Daemon ready. Waiting for invocations...")

        # Background loop: pull open cookrew tasks as a reliable fallback,
        # then run planner for empty bundles. The A2A watcher still gives
        # instant delivery when krewhub dispatches an invocation, but tasks
        # created directly by cookrew-beta enter as normal open tasks with
        # assigned_runtime_id and must be claimed from this loop.
        try:
            while True:
                await self._poll_claimable_tasks()
                await self._plan_empty_bundles()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            click.echo("  Daemon shutting down...")
            await self._watcher.stop()
            if self._running_tasks:
                click.echo(f"  Waiting for {len(self._running_tasks)} running task(s)...")
            for job in list(self._task_jobs):
                job.cancel()
            if self._task_jobs:
                await asyncio.gather(*self._task_jobs, return_exceptions=True)
            for hb in self._heartbeats:
                await hb.stop()
            for rt_hb in self._runtime_heartbeats:
                await rt_hb.stop()
            raise

    # ------------------------------------------------------------------
    # A2A invocation handler (called by SSEWatcher)
    # ------------------------------------------------------------------

    async def _handle_invocation(self, payload: dict) -> dict | None:
        """Handle an A2A invocation from krewhub's hub gateway.

        This is the callback registered with SSEWatcher. It receives
        the normalized invocation payload, extracts the task context,
        runs it through the harness, and returns the result dict.
        SSEWatcher then POSTs this to /a2a/respond automatically.

        Returns:
            Result dict on success, or raises on failure (SSEWatcher
            catches exceptions and POSTs error to /a2a/respond).
        """
        # Method dispatch — Invocation Contract §10.3 introduced
        # method="delegate" for brain-to-brain delegate calls without a
        # task lifecycle. Route those to the dedicated handler before
        # we even look at the legacy task-shaped params.
        if payload.get("method") == "delegate":
            from krewcli.daemon.delegate_handler import handle_delegate_invocation
            return await handle_delegate_invocation(
                payload, self._backends, working_dir=self._working_dir,
            )

        params = payload.get("params", {})
        message = params.get("message", {})
        metadata = message.get("metadata", {})

        task_id = metadata.get("task_id", "")
        bundle_id = metadata.get("bundle_id", "")
        agent_name = payload.get("agent_name", "")

        # Extract prompt text from A2A message parts
        parts = message.get("parts", [])
        prompt = "\n".join(
            p.get("text", "") for p in parts if p.get("kind") == "text" or "text" in p
        )

        if not task_id:
            logger.warning("invocation: no task_id in metadata, skipping")
            return {"text": "no task_id in metadata"}

        # Resolve backend — prefer the agent named in the invocation
        backend_name = agent_name if agent_name in self._backends else next(iter(self._backends))
        agent_id = self._agent_ids.get(backend_name, f"{backend_name}@{self._owner}")

        click.echo(f"  → A2A invocation: task {task_id[:12]} via {backend_name}")

        # Fetch task metadata from krewhub for title/description
        try:
            task_detail = await self._client.get_task(task_id)
        except Exception:
            task_detail = {}

        # If prompt is empty, use the task description
        if not prompt.strip():
            prompt = _build_prompt({
                "title": task_detail.get("title", ""),
                "description": task_detail.get("description", ""),
                "bundle_prompt": prompt,
            })

        self._running_tasks.add(task_id)
        try:
            result = await self._execute_task(
                backend_name=backend_name,
                agent_id=agent_id,
                task_id=task_id,
                bundle_id=bundle_id,
                prompt=prompt,
                task_detail=task_detail,
                metadata=metadata,
            )
            status = "done" if result.success else "blocked"
            click.echo(f"  ✓ Task {task_id[:12]} {status}: {result.summary[:80]}")
            return {"text": result.summary[:4096]}

        except Exception:
            logger.exception("invocation: failed for task %s", task_id)
            try:
                await self._client.update_task_status(
                    task_id, "blocked",
                    blocked_reason="Daemon execution error",
                )
            except Exception:
                pass
            raise
        finally:
            self._running_tasks.discard(task_id)

    # ------------------------------------------------------------------
    # Cookrew task polling fallback
    # ------------------------------------------------------------------

    async def _poll_claimable_tasks(self) -> None:
        """Claim and execute open tasks created directly through cookrew-beta."""
        if len(self._running_tasks) >= self._max_concurrent:
            return

        try:
            tasks = await self._client.poll_claimable_tasks(self._recipe_id)
        except Exception:
            logger.debug("poll_claimable_tasks failed", exc_info=True)
            return

        # Per-task claim-failure cooldown so we don't hammer /claim with
        # 400s every poll cycle when a task is structurally unclaimable
        # (orphan from a stale runtime, already-claimed-elsewhere, etc.)
        if not hasattr(self, "_claim_failure_count"):
            self._claim_failure_count = {}  # type: ignore[attr-defined]
            self._claim_failure_until = {}  # type: ignore[attr-defined]

        loop_now = asyncio.get_event_loop().time()

        for task in tasks:
            if len(self._running_tasks) >= self._max_concurrent:
                return

            task_id = task.get("id")
            if not task_id or task_id in self._running_tasks:
                continue

            cooldown_until = self._claim_failure_until.get(task_id, 0)  # type: ignore[attr-defined]
            if cooldown_until > loop_now:
                continue

            backend_name = self._select_backend_for_task(task)
            if backend_name is None:
                continue
            agent_id = self._agent_ids.get(
                backend_name, f"{backend_name}@{self._owner}",
            )

            try:
                claimed = await self._client.claim_task(task_id, agent_id)
            except Exception:
                fails = self._claim_failure_count.get(task_id, 0) + 1  # type: ignore[attr-defined]
                self._claim_failure_count[task_id] = fails  # type: ignore[attr-defined]
                # Exponential backoff capped at 5 min:
                # 1: 30s, 2: 60s, 3: 120s, 4: 240s, 5+: 300s
                backoff = min(300, 30 * (2 ** (fails - 1)))
                self._claim_failure_until[task_id] = loop_now + backoff  # type: ignore[attr-defined]
                logger.debug(
                    "claim failed for task %s via %s (fail #%d, cooldown %ds)",
                    task_id, agent_id, fails, backoff,
                    exc_info=True,
                )
                continue
            else:
                # Successful claim resets the cooldown for this task.
                self._claim_failure_count.pop(task_id, None)  # type: ignore[attr-defined]
                self._claim_failure_until.pop(task_id, None)  # type: ignore[attr-defined]

            task_detail = {**task, **claimed}
            self._running_tasks.add(task_id)
            job = asyncio.create_task(
                self._run_claimed_task(task_detail, backend_name, agent_id),
                name=f"cookrew-task:{task_id}",
            )
            self._task_jobs.add(job)
            job.add_done_callback(self._task_jobs.discard)

    def _select_backend_for_task(self, task: dict) -> str | None:
        """Pick the local backend that should execute an open task."""
        assigned_runtime_id = task.get("assigned_runtime_id")
        if assigned_runtime_id:
            for name, runtime_id in self._runtime_ids.items():
                if runtime_id == assigned_runtime_id:
                    return name

        assigned_agent_id = task.get("assigned_agent_id")
        if assigned_agent_id:
            backend_name = str(assigned_agent_id).split("@", 1)[0]
            if backend_name in self._backends:
                return backend_name

        # Cookrew A2 tasks are protected by account/runtime ownership at
        # the claim endpoint. If the task points at a stale runtime row,
        # falling back to a local backend lets the current daemon rescue
        # the open task instead of leaving it permanently UNCLAIMED.
        return next(iter(self._backends), None)

    async def _run_claimed_task(
        self, task: dict, backend_name: str, agent_id: str,
    ) -> None:
        task_id = task["id"]
        try:
            prompt = _build_prompt(task)
            result = await self._execute_task(
                backend_name=backend_name,
                agent_id=agent_id,
                task_id=task_id,
                bundle_id=task.get("bundle_id", ""),
                prompt=prompt,
                task_detail=task,
                metadata={"recipe_id": task.get("recipe_id", self._recipe_id)},
            )
            status = "done" if result.success else "blocked"
            click.echo(
                f"  ✓ Cookrew task {task_id[:12]} {status}: "
                f"{result.summary[:80]}",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cookrew task: failed for task %s", task_id)
            try:
                await self._client.update_task_status(
                    task_id, "blocked",
                    blocked_reason="Daemon execution error",
                )
            except Exception:
                pass
        finally:
            self._running_tasks.discard(task_id)

    async def _execute_task(
        self,
        *,
        backend_name: str,
        agent_id: str,
        task_id: str,
        bundle_id: str,
        prompt: str,
        task_detail: dict,
        metadata: dict | None = None,
    ):
        backend = self._backends[backend_name]
        metadata = metadata or {}
        async with self._semaphore:
            session = Session(self._client, task_id, agent_id)
            execenv = ExecutionEnvironment(
                base_dir=self._working_dir,
                task_id=task_id,
                bundle_id=bundle_id,
                repo_url=metadata.get("repo_url", self._repo_url),
                branch=metadata.get("branch", self._branch),
                sandbox_id=task_detail.get("sandbox_id"),
            )

            harness = Harness(self._client)
            # Surface krewhub URL + JWT to the agent's env so the
            # krewcli-bridge MCP server can call back when the brain
            # invokes `delegate(...)`. Without these, claude.py's MCP
            # wiring guard skips and the brain has no `delegate` tool —
            # which makes it reach for AskUserQuestion (denied) and
            # then hallucinate operator answers.
            from krewcli.auth.token_store import load_token
            inner = getattr(self._client, "_client", None)
            base_url = getattr(inner, "base_url", "") if inner is not None else ""
            krewhub_url = str(base_url).rstrip("/") if base_url else ""
            session_token = load_token() or ""
            return await harness.execute(
                backend=backend,
                session=session,
                execenv=execenv,
                prompt=prompt,
                task_id=task_id,
                task_title=task_detail.get("title", ""),
                task_description=task_detail.get("description", ""),
                recipe_id=metadata.get("recipe_id", self._recipe_id),
                krewhub_url=krewhub_url,
                session_token=session_token,
                bundle_id=bundle_id,
            )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def _register_and_heartbeat(self) -> None:
        """Register each backend as an agent in krewhub and start heartbeats.

        Agents are registered with endpoint_url pointing to krewhub's own
        A2A hub gateway (not the daemon's local port). This way krewhub's
        GraphRunnerController dispatches to the hub, which stores the
        invocation for SSEWatcher pickup. No NAT traversal needed.

        We register both a presence row (agent_presence — what krewhub
        uses for routing) AND a runtime row (agent_runtimes — what
        cookrew-beta's roster reads). Without the runtime row, the SPA's
        Hire-Agent flow only ever sees stale paired-but-not-running
        runtimes; tasks created via the SPA auto-bind to those instead
        of this live daemon.
        """
        from krewcli.auth.token_store import load_record
        record = load_record() or {}
        account_id = record.get("account_id")

        for name in self._backends:
            agent_id = self._agent_ids[name]
            info = BACKEND_INFO.get(name, {})
            display_name = info.get("display_name", name)
            capabilities = info.get("capabilities", ["claim"])

            # endpoint_url points to krewhub's A2A hub gateway so the
            # GraphRunnerController dispatches go through the mailbox.
            hub_base = self._client._client.base_url.__str__().rstrip("/")
            endpoint_url = f"{hub_base}/a2a/{self._owner}/{name}"

            try:
                await self._client.register_agent(
                    agent_id=agent_id,
                    cookbook_id=self._cookbook_id,
                    display_name=display_name,
                    capabilities=capabilities,
                    max_concurrent_tasks=self._max_concurrent,
                    endpoint_url=endpoint_url,
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
                endpoint_url=endpoint_url,
            )
            hb.start()
            self._heartbeats.append(hb)

            # Runtime registration — surfaces this daemon in the SPA's
            # /agents/runtimes feed so the user sees a live "claude@krew"
            # row in the party drawer (not just stale paired runtimes).
            if not account_id:
                continue
            try:
                runtime = await self._client.register_runtime(
                    agent_id=agent_id,
                    account_id=account_id,
                    daemon_version="krewcli-daemon",
                    provider=name,
                    host_info=_host_info(endpoint_url),
                )
                runtime_id = runtime.get("id")
                if runtime_id:
                    self._runtime_ids[name] = runtime_id
                    rt_hb = RuntimeHeartbeat(
                        client=self._client,
                        runtime_id=runtime_id,
                        interval=self._heartbeat_interval,
                    )
                    rt_hb.start()
                    self._runtime_heartbeats.append(rt_hb)
            except Exception:
                logger.warning(
                    "Runtime registration failed for %s — SPA roster won't "
                    "see this daemon as live, but task execution still works",
                    name,
                )

    # ------------------------------------------------------------------
    # Planning: detect empty bundles and generate graph code
    # ------------------------------------------------------------------

    async def _plan_empty_bundles(self) -> None:
        """Detect bundles that need codegen planning and generate graph code."""
        if self._planning_bundle:
            return

        try:
            bundles = await self._client.list_bundles(self._recipe_id)
        except Exception:
            return

        for bundle in bundles:
            if bundle.get("status") != "open":
                continue
            bundle_id = bundle["id"]
            try:
                detail = await self._client.get_bundle(bundle_id)
            except Exception:
                continue
            b = detail.get("bundle", detail)
            if b.get("graph_code"):
                continue
            tasks = detail.get("tasks", [])
            if tasks:
                continue

            prompt_text = bundle.get("prompt", "") or b.get("prompt", "")
            if not prompt_text:
                continue

            self._planning_bundle = bundle_id
            backend = next(iter(self._backends.values()))
            agents_summary = ", ".join(
                BACKEND_INFO.get(n, {}).get("display_name", n)
                for n in self._backends
            )

            click.echo(f"  📋 Generating graph code for bundle {bundle_id[:12]}...")

            try:
                from krewcli.daemon.planner import plan_bundle
                ok = await plan_bundle(
                    backend=backend,
                    client=self._client,
                    bundle_id=bundle_id,
                    user_prompt=prompt_text,
                    working_dir=self._working_dir,
                    agents_summary=agents_summary,
                )
                if ok:
                    click.echo(f"  📋 Graph attached to bundle {bundle_id[:12]}")
                else:
                    click.echo(f"  ⚠ Planning failed for bundle {bundle_id[:12]}")
            except Exception:
                logger.exception("plan: failed for bundle %s", bundle_id)
            finally:
                self._planning_bundle = None
            return


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
