from __future__ import annotations

from a2a.types import TaskState

from krewcli.a2a.card import build_agent_card
from krewcli.a2a.executor import _new_status_event


def test_build_agent_card_single_agent():
    card = build_agent_card("127.0.0.1", 9999, ["codex"])
    assert card.name == "KrewCLI Agent Server"
    assert len(card.skills) == 1
    assert card.skills[0].id == "codex"
    assert card.url == "http://127.0.0.1:9999"


def test_build_agent_card_multiple_agents():
    card = build_agent_card("localhost", 8000, ["codex", "claude", "bub"])
    assert len(card.skills) == 3
    skill_ids = [s.id for s in card.skills]
    assert "codex" in skill_ids
    assert "claude" in skill_ids
    assert "bub" in skill_ids


def test_build_agent_card_unknown_agent_ignored():
    card = build_agent_card("localhost", 8000, ["codex", "nonexistent"])
    assert len(card.skills) == 1
    assert card.skills[0].id == "codex"


def test_agent_card_has_streaming():
    card = build_agent_card("localhost", 9999, ["claude"])
    assert card.capabilities.streaming is True


def test_new_status_event_sets_final_flag():
    working = _new_status_event("task_1", "ctx_1", TaskState.working, final=False)
    completed = _new_status_event("task_1", "ctx_1", TaskState.completed, final=True)

    assert working.final is False
    assert working.status.state == TaskState.working
    assert completed.final is True
    assert completed.status.state == TaskState.completed
