"""Unit tests for orch prompt construction (gap 5, C4)."""

from __future__ import annotations

from krewcli.daemon.orch_prompt import (
    build_orch_prompt,
    extract_child_reports,
    extract_orch_turns,
)
from krewcli.daemon.orch_subtree import build_subtree


def _report_event(seq, from_task, report, link_id="lnk_1"):
    return {
        "type": "agent_reply",
        "actor_type": "human",
        "seq": seq,
        "payload": {
            "kind": "subagent_report",
            "from_task": from_task,
            "link_id": link_id,
            "report": report,
        },
    }


def test_extract_child_reports_pulls_subagent_reports():
    events = [
        {"type": "agent_reply", "actor_type": "agent", "seq": 1,
         "payload": {"text": "spawning"}},
        _report_event(5, "B", {"status": "done", "prs": ["pr/1"]}),
        {"type": "milestone", "seq": 6, "payload": {}},
        _report_event(8, "C", {"status": "blocked", "blockers": ["needs key"]}),
    ]
    reports = extract_child_reports(events)
    assert [r.from_task for r in reports] == ["B", "C"]
    assert reports[0].seq == 5
    assert "prs=pr/1" in reports[0].summary()
    assert "blockers=needs key" in reports[1].summary()


def test_extract_orch_turns_roles():
    events = [
        {"type": "agent_reply", "actor_type": "agent", "actor_id": "orch@me",
         "seq": 1, "payload": {"text": "I spawned B and C"}},
        _report_event(2, "B", {"status": "done"}),  # skipped — renders as report
        {"type": "agent_reply", "actor_type": "human", "actor_id": "op",
         "seq": 3, "payload": {"text": "use main branch"}},
    ]
    turns = extract_orch_turns(events, "orch@me")
    assert turns == [("ORCH", "I spawned B and C"), ("HUMAN", "use main branch")]


def test_first_turn_prompt_asks_for_decomposition():
    root = {"id": "A", "title": "Ship feature X",
            "brief_json": {"goal": "Ship X", "deliverable": "merged PR"}}
    subtree = build_subtree("A", "BND", {"A": root}, [])
    prompt = build_orch_prompt(
        root_task=root, subtree=subtree, new_reports=[],
        prior_turns=[], first_turn=True,
    )
    assert "Decompose" in prompt
    assert "GOAL: Ship X" in prompt
    assert "DELIVERABLE: merged PR" in prompt
    assert "no children spawned yet" in prompt


def test_later_turn_prompt_surfaces_new_reports():
    root = {"id": "A", "title": "Ship X", "description": "do the thing"}
    tasks = {
        "A": root,
        "B": {"id": "B", "status": "done", "title": "child B"},
    }
    links = [{"id": "l1", "from_task_id": "A", "to_task_id": "B",
              "kind": "subagent", "created_by_task": "A", "revoked_at": None}]
    subtree = build_subtree("A", "BND", tasks, links)
    reports = extract_child_reports([_report_event(5, "B", {"status": "done", "prs": ["pr/9"]})])

    prompt = build_orch_prompt(
        root_task=root, subtree=subtree, new_reports=reports,
        prior_turns=[("ORCH", "spawned B")], first_turn=False,
    )
    # v3: reports are framed as UNTRUSTED DATA, not a bare turn.
    assert "UNTRUSTED DATA" in prompt
    assert "BEGIN SUBAGENT_REPORT" in prompt
    assert "pr/9" in prompt
    assert "ORCH: spawned B" in prompt
    # Subtree table shows B as done.
    assert "done" in prompt
    # all children done → completion hint present
    assert "do NOT spawn more work" in prompt
