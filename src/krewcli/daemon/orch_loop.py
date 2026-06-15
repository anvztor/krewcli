"""Orch-agent loop — the Row-0 LLM brain (gap 5, C1/C2/C4/C5).

Where ``DaemonLoop`` is the kubelet that runs *workers* (claim a task,
execute it once, exit), ``OrchLoop`` is the *orchestrator*: a persistent
LLM session bound to one task A that **does not exit when a child
finishes**. It:

  C1  claims A, registers as a privileged orch runtime, and stays alive
      turn after turn (observe → plan → spawn → reconcile);
  C2  subscribes to a multi-task SSE stream (``watch?channel=task:*``)
      so it wakes on B/C events it didn't create directly;
  C4  reads child ``subagent_report`` turns krewhub projects onto A's
      tape and feeds them into the next turn as input;
  C5  level-triggered subtree reconcile that logs drift awareness
      without double-spawning what krewhub's mechanical reconciler
      already handles.

The brain drives everything through the SAME public API a human uses —
``spawn_subtask`` → ``POST /tasks/A/links`` (the C3 MCP tool). No new
krewhub authz surface: the orch-agent acts as task A (owner-inherited).

Gated by ``KREWCLI_ORCH_AGENT`` + the ``--orch`` daemon flag.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import click

from krewcli.backend.protocol import Backend
from krewcli.daemon.orch_prompt import (
    ChildReport,
    build_orch_prompt,
    extract_child_reports,
    extract_orch_turns,
)
from krewcli.daemon.orch_subtree import SubtreeView, reconcile_subtree
from krewcli.daemon.session import Session
from krewcli.gateway.identity import _get_owner_label, _make_agent_id
from krewcli.presence.heartbeat import RuntimeHeartbeat

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Provider tag for the orch runtime row — distinguishes the brain from
# worker runtimes in /agents/runtimes.
ORCH_PROVIDER = "orch"


class OrchLoop:
    """Persistent orchestrator bound to one root task A.

    Usage::

        loop = OrchLoop(client, backend, task_id="A", working_dir="/repo")
        await loop.run()   # runs until A is cancelled or loop is stopped
    """

    def __init__(
        self,
        client: "KrewHubClient",
        backend: Backend,
        task_id: str,
        working_dir: str,
        *,
        poll_interval: float = 5.0,
        heartbeat_interval: int = 15,
        max_turns: int | None = None,
    ) -> None:
        self._client = client
        self._backend = backend
        self._task_id = task_id
        self._working_dir = working_dir
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._max_turns = max_turns

        self._owner = _get_owner_label()
        self._agent_id = _make_agent_id(ORCH_PROVIDER, self._owner)
        self._bundle_id = ""
        self._cookbook_id = ""

        # Turn-trigger bookkeeping.
        self._turn_count = 0
        self._last_report_seq = 0
        self._acked_terminal_sig: frozenset[tuple[str, str]] = frozenset()

        # Watch / wake plumbing (C2).
        self._wake = asyncio.Event()
        self._last_watch_seq = 0
        self._stop = False
        self._runtime_id: str | None = None
        self._rt_heartbeat: RuntimeHeartbeat | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point. Runs until A is cancelled or stopped."""
        task = await self._client.get_task(self._task_id)
        self._bundle_id = task.get("bundle_id", "")
        if not self._bundle_id:
            raise click.ClickException(
                f"task {self._task_id} has no bundle_id; cannot orchestrate"
            )
        try:
            bundle_detail = await self._client.get_bundle(self._bundle_id)
            b = bundle_detail.get("bundle", bundle_detail)
            self._cookbook_id = b.get("cookbook_id", "") or ""
        except Exception:
            logger.debug("orch: could not resolve cookbook for bundle %s", self._bundle_id)

        click.echo(f"  Orch-agent starting (owner={self._owner})")
        click.echo(f"  Root task: {self._task_id}  bundle={self._bundle_id}")
        click.echo(f"  Backend:   {self._backend.name}")

        await self._register_runtime()
        await self._claim_root()
        try:
            await self._client.update_task_status(self._task_id, "working")
        except Exception:
            logger.warning("orch: failed to mark root %s working", self._task_id)

        # First turn: decompose the goal.
        await self._tick(first_turn=True)

        # C2 — start the multi-task SSE watcher (best-effort, instant wake).
        watcher = asyncio.create_task(self._watch_subtree(), name="orch-watch")
        click.echo("  Orch-agent ready. Watching subtree...")

        try:
            while not self._stop:
                if await self._root_terminal():
                    click.echo("  Root task is terminal; orch-agent retiring.")
                    break
                if self._max_turns is not None and self._turn_count >= self._max_turns:
                    click.echo("  Reached max turns; orch-agent retiring.")
                    break
                # Wait for a wake (SSE) or fall through on the poll interval.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                await self._tick(first_turn=False)
        except asyncio.CancelledError:
            click.echo("  Orch-agent shutting down...")
            raise
        finally:
            self._stop = True
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
            if self._rt_heartbeat is not None:
                await self._rt_heartbeat.stop()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    # ------------------------------------------------------------------
    # Registration + claim
    # ------------------------------------------------------------------

    async def _register_runtime(self) -> None:
        """Register the orch-agent as a privileged runtime + heartbeat.

        Acts as task A's owner (owner-inherited); visible in
        /agents/runtimes with provider=orch so the board shows the brain
        as a live runtime distinct from workers.
        """
        from krewcli.auth.token_store import account_id_from_token, load_token

        account_id = account_id_from_token(load_token())
        if not account_id:
            logger.warning(
                "orch: cannot resolve account_id from JWT — runtime row "
                "will be absent (orchestration still works)"
            )
            return
        try:
            runtime = await self._client.register_runtime(
                agent_id=self._agent_id,
                account_id=account_id,
                daemon_version="krewcli-orch",
                provider=ORCH_PROVIDER,
                host_info={"runtime": "krewcli-orch-agent", "root_task": self._task_id},
            )
            self._runtime_id = runtime.get("id")
            if self._runtime_id:
                self._rt_heartbeat = RuntimeHeartbeat(
                    client=self._client,
                    runtime_id=self._runtime_id,
                    interval=self._heartbeat_interval,
                )
                self._rt_heartbeat.start()
                click.echo(f"  Registered orch runtime ({self._agent_id})")
        except Exception:
            logger.warning(
                "orch: runtime registration failed — board won't show the "
                "brain as live, but orchestration still works",
                exc_info=True,
            )

    async def _claim_root(self) -> None:
        """Claim A so the brain owns it. Best-effort: A may already be
        claimed by this agent (re-attach) or be open."""
        try:
            await self._client.claim_task(self._task_id, self._agent_id)
            click.echo(f"  Claimed root task {self._task_id[:12]}")
        except Exception:
            logger.debug(
                "orch: claim of root %s failed (may already be owned); continuing",
                self._task_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Observe → decide → turn
    # ------------------------------------------------------------------

    async def _tick(self, *, first_turn: bool) -> None:
        """Observe the subtree; run a brain turn iff something warrants it."""
        subtree = await reconcile_subtree(
            self._client, self._task_id, self._bundle_id,
        )
        events = await self._client.get_task_events(self._task_id, limit=400)

        terminal_sig = subtree.terminal_signature()
        drift = terminal_sig != self._acked_terminal_sig

        # C5 — log drift awareness (separate from the brain's reasoning) so
        # there's a tape record that the loop noticed a child go terminal,
        # even before/independent of what the brain decides.
        if drift and not first_turn:
            await self._log_drift(subtree)

        # C4 — gather the reports the brain hasn't seen. Two sources, so a
        # report is NEVER lost to the bounded event-tape window: (a) the
        # subagent_report turns krewhub projected onto A's tape (rich
        # payload), and (b) for any newly-terminal child whose report
        # isn't in that window, the child's own task record. Drift in the
        # reliable subtree state — not the lossy tape — gates consumption.
        tape_new = [
            r for r in extract_child_reports(events)
            if r.seq > self._last_report_seq
        ]
        new_reports = await self._collect_new_reports(subtree, tape_new)

        warranted = first_turn or bool(new_reports)
        if not warranted:
            return

        prior_turns = extract_orch_turns(events, self._agent_id)
        root_task = await self._client.get_task(self._task_id)
        prompt = build_orch_prompt(
            root_task=root_task,
            subtree=subtree,
            new_reports=new_reports,
            prior_turns=prior_turns,
            first_turn=first_turn,
        )
        await self._run_turn(prompt)

        # Advance the cursors ONLY over what we actually showed the brain:
        # the tape cursor over consumed tape reports (monotonic), and the
        # terminal signature so each (child, status) surfaces exactly once.
        if tape_new:
            self._last_report_seq = max(r.seq for r in tape_new)
        self._acked_terminal_sig = terminal_sig

    async def _collect_new_reports(
        self, subtree: SubtreeView, tape_new: list[ChildReport],
    ) -> list[ChildReport]:
        """Union of fresh tape reports + synthesized reports for any
        newly-terminal child the tape window didn't carry.

        Keyed off the subtree's terminal drift (reliable) rather than the
        event tape (windowed), so a worker's Report can't be stranded by a
        chatty orchestrator pushing it out of the last-400 window.
        """
        reports = list(tape_new)
        on_tape = {r.from_task for r in tape_new}
        prev = dict(self._acked_terminal_sig)
        for child in subtree.terminal():
            if prev.get(child.task_id) == child.status:
                continue  # already surfaced this (child, status)
            if child.task_id in on_tape:
                continue  # tape already carries a fresh report for it
            report = await self._fetch_child_report(child.task_id, child.status)
            reports.append(ChildReport(
                from_task=child.task_id, link_id=child.link_id,
                report=report, seq=0,
            ))
        return reports

    async def _fetch_child_report(self, child_id: str, status: str) -> dict:
        """Read a terminal child's structured Report from its task row."""
        try:
            child = await self._client.get_task(child_id)
        except Exception:
            return {"status": status}
        report = child.get("report")
        if report is None:
            report = child.get("report_json")
        if isinstance(report, str):
            import json
            try:
                report = json.loads(report)
            except Exception:
                report = None
        if isinstance(report, dict):
            return report
        return {"status": status}

    async def _run_turn(self, prompt: str) -> None:
        """Run one brain turn: stream the LLM's output onto A's tape.

        Unlike the worker harness this NEVER flips A's status to done —
        the orchestrator stays ``working`` across turns. Spawns happen
        as a side effect of the brain calling ``spawn_subtask`` (which
        the bridge routes to ``POST /tasks/A/links``).
        """
        self._turn_count += 1
        click.echo(f"  ▶ orch turn {self._turn_count}")
        session = Session(self._client, self._task_id, self._agent_id)
        env = self._turn_env()
        backend_session = None
        try:
            backend_session = await self._backend.execute(
                prompt, self._working_dir, env=env,
            )
            async for msg in backend_session.messages_iter():
                await session.append_from_backend(msg)
            result = await backend_session.result()
            if result.usage:
                await session.report_usage(result.usage)
        except Exception:
            logger.exception("orch: turn %d failed", self._turn_count)
        finally:
            # Cancel the backend's runner + child process so a never-exiting
            # orchestrator doesn't leak a subprocess per failed/cancelled turn.
            if backend_session is not None:
                aclose = getattr(backend_session, "aclose", None)
                if aclose is not None:
                    await aclose()
            await session.flush()

    def _turn_env(self) -> dict[str, str]:
        """Env overlay for the brain subprocess.

        ``KREWCLI_ORCH_AGENT=1`` flips the claude backend into orch mode
        (unlocks spawn_subtask, swaps in the orchestrator system note)
        and is forwarded into the bridge so spawn_subtask is advertised.
        ``KREWHUB_TASK_ID=A`` makes spawn_subtask parent its children to
        A — the brain cannot vary it.
        """
        from krewcli.auth.token_store import load_token

        inner = getattr(self._client, "_client", None)
        base_url = getattr(inner, "base_url", "") if inner is not None else ""
        krewhub_url = str(base_url).rstrip("/") if base_url else ""
        env = {
            "KREWCLI_ORCH_AGENT": "1",
            "KREWHUB_TASK_ID": self._task_id,
            "KREWHUB_BUNDLE_ID": self._bundle_id,
            "KREWHUB_COOKBOOK_ID": self._cookbook_id,
            "KREWHUB_PARENT_TAPE_ID": self._task_id,
        }
        if krewhub_url:
            env["KREWHUB_URL"] = krewhub_url
        token = load_token() or ""
        if token:
            env["KREWHUB_SESSION_TOKEN"] = token
        return env

    async def _log_drift(self, subtree: SubtreeView) -> None:
        """Append a milestone noting which children newly went terminal."""
        prev = dict(self._acked_terminal_sig)
        newly = [
            c for c in subtree.terminal()
            if prev.get(c.task_id) != c.status
        ]
        if not newly:
            return
        summary = ", ".join(f"{c.task_id[:8]}→{c.status}" for c in newly)
        try:
            await self._client.post_event(
                self._task_id,
                "milestone",
                self._agent_id,
                body=f"subtree drift: {summary}",
                payload={"kind": "orch.subtree_drift", "children": [
                    {"task_id": c.task_id, "status": c.status} for c in newly
                ]},
            )
        except Exception:
            logger.debug("orch: failed to log drift awareness", exc_info=True)

    # ------------------------------------------------------------------
    # C2 — multi-task SSE subscription
    # ------------------------------------------------------------------

    async def _watch_subtree(self) -> None:
        """Subscribe to ``watch?channel=task:*`` and nudge the loop awake.

        Dedups by ``seq`` and filters to A's bundle (links can't cross
        bundles in v1, so A's whole subtree lives here). On disconnect it
        reconnects with ``since=last_seq`` so no event is missed — the
        poll fallback in ``run`` covers any gap regardless.
        """
        backoff = 1.0
        while not self._stop:
            try:
                got_event = False
                async for ev in self._client.watch(
                    channel="task:*", since=self._last_watch_seq,
                ):
                    if self._stop:
                        break
                    got_event = True
                    seq = int(ev.get("seq", 0) or 0)
                    if seq <= self._last_watch_seq:
                        continue  # dedup
                    self._last_watch_seq = seq
                    obj = ev.get("object") or {}
                    bundle_id = obj.get("bundle_id") if isinstance(obj, dict) else None
                    # Resource events carry bundle_id; nested-event payloads
                    # may not — when absent, nudge and let _tick filter.
                    if bundle_id and bundle_id != self._bundle_id:
                        continue
                    self._wake.set()
                # Stream ended. A productive stream resets the backoff; an
                # immediately-EOFing one ramps up to a 30s ceiling so we
                # don't hot-loop reconnects. The poll fallback covers gaps.
                backoff = 1.0 if got_event else min(backoff * 2, 30.0)
                if not self._stop:
                    await asyncio.sleep(min(self._poll_interval, backoff))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("orch: watch stream dropped, reconnecting", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _root_terminal(self) -> bool:
        """True when A was cancelled/closed out from under the brain."""
        try:
            task = await self._client.get_task(self._task_id)
        except Exception:
            return False
        return task.get("status") in ("cancelled", "done")
