from __future__ import annotations

from krewcli.agents.base import AgentDeps, HarnessConfig


def orchestrator_preset(
    working_dir: str,
    repo_url: str = "",
    branch: str = "main",
) -> AgentDeps:
    """AgentDeps preset for the orchestrator role."""
    return AgentDeps(
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        system_prompt=(
            "You are an orchestrator agent. Your job is to decompose complex "
            "requests into a pydantic-graph workflow, then dispatch each step "
            "to specialized online agents."
        ),
        harness=HarnessConfig(timeout=600, max_retries=3),
        context={"role": "orchestrator"},
    )


def verifier_preset(
    working_dir: str,
    repo_url: str = "",
    branch: str = "main",
) -> AgentDeps:
    """AgentDeps preset for the verifier role (future)."""
    return AgentDeps(
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        system_prompt="You are a verification agent. Review outputs for correctness.",
        harness=HarnessConfig(timeout=120),
        context={"role": "verifier"},
    )
