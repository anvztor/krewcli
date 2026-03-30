from __future__ import annotations

from krewcli.agents.base import AgentDeps
from krewcli.agents.bub_agent import create_bub_agent
from krewcli.agents.claude_agent import create_claude_agent
from krewcli.agents.codex_agent import create_codex_agent


def test_local_agent_wrappers_do_not_require_provider_keys():
    deps = AgentDeps(working_dir=".", repo_url="", branch="main")

    for factory in (create_codex_agent, create_claude_agent, create_bub_agent):
        agent = factory()
        assert agent is not None
        assert callable(agent.run)
        assert deps.branch == "main"
