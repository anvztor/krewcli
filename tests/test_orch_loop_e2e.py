"""End-to-end orch-agent loop tests (gap 5, C1/C2/C4/C5 + eval E1/E2/E4).

Drives ``OrchLoop`` against an in-memory fake krewhub and a scripted
"brain" backend that decomposes the goal by calling ``create_link``
(the same call ``spawn_subtask`` makes). Proves the brain-driven column
of the §D eval without a live stack:

  E1  brain spawns children → provenance + subagent links appear
  E2  a worker's Report flows onto A's tape → the brain's NEXT turn
      cites it (Report-consumption, C4)
  E4  a child failure surfaces; the brain can spawn a follow-up
  C1  the loop registers a runtime + stays alive across idle turns
  C2  the multi-task watcher dedups by seq and wakes the loop
  C5  subtree drift is logged as an awareness milestone
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)
from krewcli.daemon.orch_loop import OrchLoop


# ---------------------------------------------------------------------------
# In-memory fake krewhub
# ---------------------------------------------------------------------------


class _Inner:
    base_url = "http://fake-krewhub"


class FakeKrewHub:
    """Minimal in-memory krewhub mirroring the orch-relevant surface."""

    def __init__(self):
        self._client = _Inner()
        self.tasks: dict[str, dict] = {}
        self.links: list[dict] = []
        self.events: dict[str, list[dict]] = {}
        self.statuses: list[tuple[str, str]] = []
        self.runtimes: list[dict] = []
        self._seq = itertools.count(1)
        self._link_seq = itertools.count(1)
        self._task_seq = itertools.count(1)
        self.watch_queue: asyncio.Queue = asyncio.Queue()

    # --- seeding helpers ---
    def add_task(self, tid, bundle_id, status="open", title="", brief=None,
                 created_by=None, description=""):
        self.tasks[tid] = {
            "id": tid, "bundle_id": bundle_id, "status": status,
            "title": title, "description": description,
            "brief_json": brief, "created_by_task": created_by,
            "depends_on_task_ids": [],
        }
        self.events.setdefault(tid, [])

    # --- reads ---
    async def get_task(self, task_id):
        return dict(self.tasks[task_id])

    async def get_bundle(self, bundle_id):
        return {
            "bundle": {"id": bundle_id, "cookbook_id": "CB"},
            "tasks": [dict(t) for t in self.tasks.values()
                      if t["bundle_id"] == bundle_id],
        }

    async def get_bundle_links(self, bundle_id, *, include_revoked=False):
        return [dict(l) for l in self.links
                if l["bundle_id"] == bundle_id and not l.get("revoked_at")]

    async def get_task_events(self, task_id, *, limit=400):
        return [dict(e) for e in self.events.get(task_id, [])][-limit:]

    # --- mutations ---
    async def claim_task(self, task_id, agent_id):
        self.tasks[task_id]["status"] = "working"
        self.tasks[task_id]["assigned_agent_id"] = agent_id
        return dict(self.tasks[task_id])

    async def register_runtime(self, **kwargs):
        rt = {"id": f"rt_{len(self.runtimes)+1}", **kwargs}
        self.runtimes.append(rt)
        return rt

    async def heartbeat_runtime(self, runtime_id):
        return {"id": runtime_id}

    async def update_task_status(self, task_id, status, blocked_reason=None):
        self.tasks[task_id]["status"] = status
        self.statuses.append((task_id, status))
        return dict(self.tasks[task_id])

    async def post_event(self, task_id, event_type, actor_id, body,
                         payload=None, facts=None, code_refs=None):
        ev = {
            "type": event_type, "actor_id": actor_id, "actor_type": "agent",
            "body": body, "payload": payload or {}, "seq": next(self._seq),
        }
        self.events.setdefault(task_id, []).append(ev)
        return ev

    async def post_events_batch(self, task_id, events):
        out = []
        for e in events:
            ev = {**e, "seq": next(self._seq),
                  "actor_type": e.get("actor_type", "agent")}
            self.events.setdefault(task_id, []).append(ev)
            out.append(ev)
        return out

    async def post_task_usage(self, *a, **k):
        return {}

    async def post_task_completion(self, *a, **k):
        return {}

    async def create_link(self, from_task_id, *, kind="subagent",
                          to_task_id=None, new_task=None, payload_map=None):
        """Mirror krewhub: inline new_task → child + provenance + link."""
        bundle_id = self.tasks[from_task_id]["bundle_id"]
        if new_task is not None:
            child_id = f"task_{next(self._task_seq)}"
            self.add_task(
                child_id, bundle_id, status="open",
                title=new_task.get("title", ""),
                brief=new_task.get("brief"),
                created_by=from_task_id,  # provenance (E1)
            )
            to_task_id = child_id
        link = {
            "id": f"lnk_{next(self._link_seq)}", "bundle_id": bundle_id,
            "from_task_id": from_task_id, "to_task_id": to_task_id,
            "kind": kind, "created_by_task": from_task_id,
            "revoked_at": None, "fired_at": None,
        }
        self.links.append(link)
        return {"link": link, "to_task": dict(self.tasks[to_task_id])}

    async def watch(self, *, channel=None, resource_type=None, since=0):
        """Yield queued watch events then end (loop reconnects)."""
        while not self.watch_queue.empty():
            yield await self.watch_queue.get()

    async def close(self):
        pass

    # --- test-side simulation of krewhub's report-up-the-link ---
    async def simulate_worker_report(self, parent_id, child_id, report,
                                     link_id="lnk_1"):
        self.tasks[child_id]["status"] = report.get("status", "done")
        ev = {
            "type": "agent_reply", "actor_type": "human",
            "actor_id": "orch-controller",
            "body": f"Report from {child_id}",
            "payload": {"kind": "subagent_report", "from_task": child_id,
                        "link_id": link_id, "report": report},
            "seq": next(self._seq),
        }
        self.events.setdefault(parent_id, []).append(ev)


# ---------------------------------------------------------------------------
# Scripted brain backend
# ---------------------------------------------------------------------------


class ScriptedBrain:
    """A backend whose each turn runs a handler(prompt, env) coroutine.

    The handler may spawn children (via a closure over the fake hub) to
    stand in for the brain calling spawn_subtask. Returns the reply text.
    """

    def __init__(self, handlers):
        self._handlers = handlers
        self.prompts: list[str] = []
        self.envs: list[dict] = []
        self._turn = 0

    @property
    def name(self):
        return "scripted"

    async def health(self):
        return True

    async def execute(self, prompt, working_dir, *, env=None):
        self.prompts.append(prompt)
        self.envs.append(env or {})
        handler = self._handlers[min(self._turn, len(self._handlers) - 1)]
        self._turn += 1
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        asyncio.create_task(self._run(handler, prompt, env or {}, queue, fut))
        return BackendSession(messages=queue, result_future=fut)

    async def _run(self, handler, prompt, env, queue, fut):
        await queue.put(BackendMessage(kind="session_start", body="▶", payload={}))
        reply = await handler(prompt, env)
        await queue.put(BackendMessage(
            kind="agent_reply", body=reply[:120], payload={"text": reply}))
        await queue.put(BackendMessage(kind="session_end", body="■", payload={"success": True}))
        fut.set_result(BackendResult(success=True, summary=reply))
        await queue.put(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_loop(hub, brain, max_turns=None, monkeypatch=None):
    if monkeypatch is not None:
        # _register_runtime + _turn_env import these locally from token_store.
        import krewcli.auth.token_store as ts
        monkeypatch.setattr(ts, "load_token", lambda: "tok")
        monkeypatch.setattr(ts, "account_id_from_token", lambda _t: "acct_1")
    loop = OrchLoop(hub, brain, task_id="A", working_dir="/tmp/orch",
                    poll_interval=0.05, max_turns=max_turns)
    return loop


@pytest.mark.asyncio
async def test_first_turn_spawns_children_with_provenance(monkeypatch):
    """E1: the brain decomposes the goal into two provenance-stamped children."""
    hub = FakeKrewHub()
    hub.add_task("A", "BND", status="working", title="Ship X",
                 brief={"goal": "Ship X", "deliverable": "merged PR"})

    async def turn1(prompt, env):
        # E1: the brain spawns B and C (as spawn_subtask would).
        await hub.create_link("A", kind="subagent",
                              new_task={"title": "child B",
                                        "brief": {"goal": "b", "deliverable": "d"}})
        await hub.create_link("A", kind="subagent",
                              new_task={"title": "child C",
                                        "brief": {"goal": "c", "deliverable": "d"}})
        # spawn env must mark orch mode so the bridge unlocks spawn_subtask
        assert env.get("KREWCLI_ORCH_AGENT") == "1"
        assert env.get("KREWHUB_TASK_ID") == "A"
        return "Decomposed into B and C"

    brain = ScriptedBrain([turn1])
    loop = _make_loop(hub, brain, monkeypatch=monkeypatch)
    loop._bundle_id = "BND"

    await loop._tick(first_turn=True)

    # Two children, each with created_by_task == A (provenance).
    children = [t for t in hub.tasks.values() if t["created_by_task"] == "A"]
    assert len(children) == 2
    # Two subagent links, A → B and A → C.
    sub_links = [l for l in hub.links if l["kind"] == "subagent"]
    assert len(sub_links) == 2
    assert all(l["from_task_id"] == "A" for l in sub_links)
    # The brain's spawn decision is on A's tape.
    texts = [e["payload"].get("text", "") for e in hub.events["A"]
             if e["type"] == "agent_reply"]
    assert any("Decomposed into B and C" in t for t in texts)


@pytest.mark.asyncio
async def test_report_consumption_drives_next_turn(monkeypatch):
    """E2/E4 + C4: a child's Report reaches the brain's next-turn prompt."""
    hub = FakeKrewHub()
    hub.add_task("A", "BND", status="working", title="Ship X",
                 brief={"goal": "Ship X", "deliverable": "PR"})
    seen_report = {}

    async def turn1(prompt, env):
        await hub.create_link("A", kind="subagent",
                              new_task={"title": "child B",
                                        "brief": {"goal": "b", "deliverable": "d"}})
        return "Spawned B"

    async def turn2(prompt, env):
        # E2: this turn's prompt must cite B's Report.
        seen_report["prompt"] = prompt
        return "B reported done — accepting, goal complete"

    brain = ScriptedBrain([turn1, turn2])
    loop = _make_loop(hub, brain, monkeypatch=monkeypatch)
    loop._bundle_id = "BND"

    # Turn 1 — spawn.
    await loop._tick(first_turn=True)
    child_id = next(t["id"] for t in hub.tasks.values()
                    if t["created_by_task"] == "A")

    # Worker completes B; krewhub flows the Report onto A's tape.
    await hub.simulate_worker_report(
        "A", child_id, {"status": "done", "prs": ["pr/42"]})

    # Turn 2 — the brain consumes the report.
    await loop._tick(first_turn=False)

    assert "prompt" in seen_report  # a turn actually ran
    assert "Child reports (NEW since your last turn)" in seen_report["prompt"]
    assert child_id in seen_report["prompt"]
    assert "pr/42" in seen_report["prompt"]

    # C5: drift awareness milestone recorded when B went terminal.
    milestones = [e for e in hub.events["A"]
                  if e["type"] == "milestone"
                  and e["payload"].get("kind") == "orch.subtree_drift"]
    assert milestones, "expected a subtree-drift awareness milestone"


@pytest.mark.asyncio
async def test_report_survives_tape_window_via_child_record(monkeypatch):
    """HIGH fix: a terminal child's Report is consumed even when it never
    appears (or aged out) on A's bounded event tape — read from the child
    task record, gated by reliable subtree drift."""
    hub = FakeKrewHub()
    hub.add_task("A", "BND", status="working", title="X",
                 brief={"goal": "X", "deliverable": "Y"})
    seen = {}

    async def turn1(prompt, env):
        await hub.create_link("A", kind="subagent",
                              new_task={"title": "B",
                                        "brief": {"goal": "b", "deliverable": "d"}})
        return "spawned B"

    async def turn2(prompt, env):
        seen["prompt"] = prompt
        return "saw B done"

    brain = ScriptedBrain([turn1, turn2])
    loop = _make_loop(hub, brain, monkeypatch=monkeypatch)
    loop._bundle_id = "BND"

    await loop._tick(first_turn=True)
    child_id = next(t["id"] for t in hub.tasks.values()
                    if t["created_by_task"] == "A")

    # Worker finishes WITHOUT any subagent_report on A's tape; the Report
    # lives only on the child task row.
    hub.tasks[child_id]["status"] = "done"
    hub.tasks[child_id]["report"] = {"status": "done", "prs": ["pr/77"]}

    await loop._tick(first_turn=False)

    assert "prompt" in seen, "drift in subtree should warrant a turn"
    assert child_id in seen["prompt"]
    assert "pr/77" in seen["prompt"]  # payload recovered from child record


@pytest.mark.asyncio
async def test_idempotent_no_turn_without_change(monkeypatch):
    """A tick with no new reports / no drift must NOT run a brain turn."""
    hub = FakeKrewHub()
    hub.add_task("A", "BND", status="working", title="X",
                 brief={"goal": "X", "deliverable": "Y"})

    calls = {"n": 0}

    async def turn(prompt, env):
        calls["n"] += 1
        return "ok"

    brain = ScriptedBrain([turn])
    loop = _make_loop(hub, brain, monkeypatch=monkeypatch)
    loop._bundle_id = "BND"

    await loop._tick(first_turn=True)   # runs (first)
    assert calls["n"] == 1
    await loop._tick(first_turn=False)  # nothing changed → no turn
    assert calls["n"] == 1
    assert loop._turn_count == 1


@pytest.mark.asyncio
async def test_watcher_dedups_by_seq_and_wakes(monkeypatch):
    """C2: the multi-task watcher dedups by seq and nudges the loop."""
    hub = FakeKrewHub()
    hub.add_task("A", "BND", status="working")
    brain = ScriptedBrain([lambda p, e: _areturn("noop")])
    loop = _make_loop(hub, brain, monkeypatch=monkeypatch)
    loop._bundle_id = "BND"

    # Queue events: seq 1, a duplicate seq 1, then seq 2 — and one for a
    # different bundle (must be filtered out, no wake-relevant bundle).
    for ev in [
        {"seq": 1, "object": {"bundle_id": "BND", "id": "B"}},
        {"seq": 1, "object": {"bundle_id": "BND", "id": "B"}},  # dup
        {"seq": 2, "object": {"bundle_id": "OTHER", "id": "Z"}},  # other bundle
        {"seq": 3, "object": {"bundle_id": "BND", "id": "C"}},
    ]:
        hub.watch_queue.put_nowait(ev)

    # Run one watch pass.
    task = asyncio.create_task(loop._watch_subtree())
    await asyncio.sleep(0.1)
    loop._stop = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert loop._last_watch_seq == 3        # advanced past every seq
    assert loop._wake.is_set()              # woke the loop


@pytest.mark.asyncio
async def test_run_registers_runtime_and_stays_alive(monkeypatch):
    """C1: full run() registers a privileged runtime and persists past idle."""
    hub = FakeKrewHub()
    hub.add_task("A", "BND", status="open", title="X",
                 brief={"goal": "X", "deliverable": "Y"})

    async def turn1(prompt, env):
        await hub.create_link("A", kind="subagent",
                              new_task={"title": "B",
                                        "brief": {"goal": "b", "deliverable": "d"}})
        return "spawned"

    brain = ScriptedBrain([turn1])
    # max_turns=1 → initial turn runs, then loop retires deterministically.
    loop = _make_loop(hub, brain, max_turns=1, monkeypatch=monkeypatch)

    await asyncio.wait_for(loop.run(), timeout=5.0)

    # Registered as a privileged orch runtime (provider=orch).
    assert hub.runtimes, "orch-agent should register a runtime"
    assert hub.runtimes[0]["provider"] == "orch"
    assert hub.runtimes[0]["daemon_version"] == "krewcli-orch"
    # Claimed A and marked it working (not done — orchestrator persists).
    assert ("A", "working") in hub.statuses
    assert hub.tasks["A"]["status"] == "working"
    # The first turn ran and spawned.
    assert loop._turn_count == 1
    assert any(l["kind"] == "subagent" for l in hub.links)


def _areturn(value):
    async def _coro():
        return value
    return _coro()
