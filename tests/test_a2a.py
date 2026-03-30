from __future__ import annotations

import pytest

from krewcli.a2a.card import build_agent_card


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
