"""Orchestration drive loop — v3 single-link model.

When a claimed task ends a brain turn with **outgoing drives links**, it
is orchestrating them: this engine drives the subtree — observe children,
consume their Report↑ as UNTRUSTED DATA, run orchestrator turns (which may
spawn more / reclaim / finalize), until the subtree is quiescent, then
mark the root done. There is no separate daemon and no mode enum: the
daemon's claim path runs the first brain turn through the Harness, checks
out-edges, and only then hands an orchestrating task to ``OrchDrive``.

Security: link content (Brief↓, Report↑) is consumed as typed/delimited
DATA envelopes (orch_prompt), secrets redacted; blast radius is capped by
``OrchConfig`` (children/depth/spawns-per-turn/turns); krewhub authz is
the backstop.
"""

from __future__ import annotations

import asyncio
import logging

from krewcli.daemon.orch_config import OrchConfig
from krewcli.daemon.orch_prompt import (
    ChildReport,
    build_orch_prompt,
    extract_child_reports,
    extract_orch_turns,
)
from krewcli.daemon.orch_subtree import SubtreeView, reconcile_subtree
from krewcli.daemon.session import Session

logger = logging.getLogger(__name__)


async def run_backend_turn(
    *, client, backend, task_id, agent_id, prompt, working_dir, env,
):
    """Run one brain turn, streaming its output onto the task's tape.

    Does NOT change task status (the caller owns the lifecycle). Always
    cancels the backend runner + child process on exit so a long-lived
    loop can't leak a subprocess per turn.
    """
    session = Session(client, task_id, agent_id)
    backend_session = None
    try:
        backend_session = await backend.execute(prompt, working_dir, env=env)
        async for msg in backend_session.messages_iter():
            await session.append_from_backend(msg)
        result = await backend_session.result()
        if result.usage:
            await session.report_usage(result.usage)
        return result
    finally:
        if backend_session is not None:
            aclose = getattr(backend_session, "aclose", None)
            if aclose is not None:
                await aclose()
        await session.flush()


class OrchDrive:
    """Drives one orchestrating task's subtree to completion."""

    def __init__(
        self,
        *,
        client,
        backend,
        task: dict,
        agent_id: str,
        working_dir: str,
        krewhub_url: str = "",
        session_token: str = "",
        cookbook_id: str = "",
        depth: int = 0,
        poll_interval: float = 5.0,
        config: OrchConfig | None = None,
    ) -> None:
        self._client = client
        self._backend = backend
        self._task = task
        self._task_id = task["id"]
        self._bundle_id = task.get("bundle_id", "")
        self._agent_id = agent_id
        self._working_dir = working_dir
        self._krewhub_url = krewhub_url
        self._session_token = session_token
        self._cookbook_id = cookbook_id
        self._depth = depth
        self._poll_interval = poll_interval
        self._cfg = config or OrchConfig.from_env()

        self._turn_count = 0
        self._last_report_seq = 0
        self._acked_terminal_sig: frozenset[tuple[str, str]] = frozenset()
        self._wake = asyncio.Event()
        self._last_watch_seq = 0
        self._stop = False
        self.summary = "orchestration complete"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drive until the subtree is quiescent, the root is terminal, or
        the turn ceiling is hit. The first brain turn already ran (Harness,
        in the daemon claim path) and produced the out-edges that brought
        us here — so we start in the watch/consume loop, not a fresh turn."""
        watcher = asyncio.create_task(self._watch_subtree(), name="orch-watch")
        try:
            while not self._stop:
                if await self._root_terminal():
                    self.summary = "root task terminal; orchestrator retired"
                    break
                if self._turn_count >= self._cfg.max_turns:
                    self.summary = f"reached max_turns={self._cfg.max_turns}"
                    logger.info("orch: %s", self.summary)
                    break
                # Observe + (maybe) run a turn, then check completion before
                # blocking — so finalizing doesn't wait a full poll interval.
                ran = await self._tick()
                if await self._maybe_finish():
                    break
                if not ran:
                    # Nothing warranted a turn; wait for a child event/wake.
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
        except asyncio.CancelledError:
            self.summary = "orchestrator cancelled (daemon shutdown)"
            raise
        finally:
            self._stop = True
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass

    async def _maybe_finish(self) -> bool:
        """Mark the root done + stop when the subtree is quiescent.

        Quiescent = no child is still making progress (every child is
        terminal: done/blocked/cancelled), or there are no children. A
        blocked child counts as quiescent — the brain had its turn to
        react to the report; if it didn't escalate or re-spawn, the
        orchestration is finished (the human can still intervene)."""
        subtree = await reconcile_subtree(self._client, self._task_id, self._bundle_id)
        if subtree.pending():
            return False
        try:
            await self._client.update_task_status(self._task_id, "done")
        except Exception:
            logger.warning("orch: failed to mark root %s done", self._task_id)
        n = len(subtree.children)
        self.summary = (
            f"goal complete — {n} subtask(s) terminal" if n
            else "goal answered without subtasks"
        )
        return True

    # ------------------------------------------------------------------
    # Observe → decide → turn
    # ------------------------------------------------------------------

    async def _tick(self) -> bool:
        """Observe; run an orchestrator turn iff a child reported. True if ran."""
        subtree = await reconcile_subtree(self._client, self._task_id, self._bundle_id)
        events = await self._client.get_task_events(self._task_id, limit=400)

        terminal_sig = subtree.terminal_signature()
        if terminal_sig != self._acked_terminal_sig:
            await self._log_drift(subtree)

        tape_new = [
            r for r in extract_child_reports(events) if r.seq > self._last_report_seq
        ]
        new_reports = await self._collect_new_reports(subtree, tape_new)
        if not new_reports:
            return False

        secrets = (self._session_token,) if self._session_token else ()
        spawn_budget = self._cfg.spawn_budget(len(subtree.children), self._depth)
        prior_turns = extract_orch_turns(events, self._agent_id)
        root_task = await self._client.get_task(self._task_id)
        prompt = build_orch_prompt(
            root_task=root_task,
            subtree=subtree,
            new_reports=new_reports,
            prior_turns=prior_turns,
            first_turn=False,
            secrets=secrets,
            spawn_budget=spawn_budget,
        )
        await self._run_turn(prompt, spawn_budget)

        if tape_new:
            self._last_report_seq = max(r.seq for r in tape_new)
        self._acked_terminal_sig = terminal_sig
        return True

    async def _collect_new_reports(
        self, subtree: SubtreeView, tape_new: list[ChildReport],
    ) -> list[ChildReport]:
        """Fresh tape reports + synthesized reports for any newly-terminal
        child the bounded tape window didn't carry (gated by reliable
        subtree drift, so a Report is never stranded)."""
        reports = list(tape_new)
        on_tape = {r.from_task for r in tape_new}
        prev = dict(self._acked_terminal_sig)
        for child in subtree.terminal():
            if prev.get(child.task_id) == child.status:
                continue
            if child.task_id in on_tape:
                continue
            report = await self._fetch_child_report(child.task_id, child.status)
            reports.append(ChildReport(
                from_task=child.task_id, link_id=child.link_id, report=report, seq=0,
            ))
        return reports

    async def _fetch_child_report(self, child_id: str, status: str) -> dict:
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
        return report if isinstance(report, dict) else {"status": status}

    async def _run_turn(self, prompt: str, spawn_budget: int) -> None:
        self._turn_count += 1
        logger.info("orch: turn %d (spawn_budget=%d)", self._turn_count, spawn_budget)
        try:
            await run_backend_turn(
                client=self._client,
                backend=self._backend,
                task_id=self._task_id,
                agent_id=self._agent_id,
                prompt=prompt,
                working_dir=self._working_dir,
                env=self._turn_env(spawn_budget),
            )
        except Exception:
            logger.exception("orch: turn %d failed", self._turn_count)

    def _turn_env(self, spawn_budget: int) -> dict[str, str]:
        """Env overlay for the orchestrator brain subprocess.

        ``KREWHUB_TASK_ID`` (=this task) makes spawn_subtask parent its
        children here (the brain cannot vary it). ``KREWCLI_ORCH_SPAWN_
        BUDGET`` caps spawns this turn. The unified tool surface (delegate
        + spawn_subtask) is wired by the backend unconditionally."""
        env = {
            "KREWCLI_ORCH_SPAWN_BUDGET": str(spawn_budget),
            "KREWHUB_TASK_ID": self._task_id,
            "KREWHUB_BUNDLE_ID": self._bundle_id,
            "KREWHUB_COOKBOOK_ID": self._cookbook_id,
            "KREWHUB_PARENT_TAPE_ID": self._task_id,
        }
        if self._krewhub_url:
            env["KREWHUB_URL"] = self._krewhub_url
        if self._session_token:
            env["KREWHUB_SESSION_TOKEN"] = self._session_token
        return env

    async def _log_drift(self, subtree: SubtreeView) -> None:
        prev = dict(self._acked_terminal_sig)
        newly = [c for c in subtree.terminal() if prev.get(c.task_id) != c.status]
        if not newly:
            return
        summary = ", ".join(f"{c.task_id[:8]}→{c.status}" for c in newly)
        try:
            await self._client.post_event(
                self._task_id, "milestone", self._agent_id,
                body=f"subtree drift: {summary}",
                payload={"kind": "orch.subtree_drift", "children": [
                    {"task_id": c.task_id, "status": c.status} for c in newly
                ]},
            )
        except Exception:
            logger.debug("orch: failed to log drift awareness", exc_info=True)

    # ------------------------------------------------------------------
    # Multi-task SSE subscription (instant wake; poll is the fallback)
    # ------------------------------------------------------------------

    async def _watch_subtree(self) -> None:
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
                        continue
                    self._last_watch_seq = seq
                    obj = ev.get("object") or {}
                    bundle_id = obj.get("bundle_id") if isinstance(obj, dict) else None
                    if bundle_id and bundle_id != self._bundle_id:
                        continue
                    self._wake.set()
                backoff = 1.0 if got_event else min(backoff * 2, 30.0)
                if not self._stop:
                    await asyncio.sleep(min(self._poll_interval, backoff))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("orch: watch dropped, reconnecting", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _root_terminal(self) -> bool:
        try:
            task = await self._client.get_task(self._task_id)
        except Exception:
            return False
        return task.get("status") in ("cancelled", "done")
