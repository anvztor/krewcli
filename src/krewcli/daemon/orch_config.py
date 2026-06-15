"""Blast-radius limits for orchestration (orch security).

An orchestrator drives spawning through an LLM, so a prompt-injection or
a runaway reasoning loop could try to fan out without bound. These caps
make any such runaway finite. They are config-driven (env) so ops can
tune them per deployment without a code change. The real backstop is
still krewhub authz (owner-inherited provenance, cycle-guard, review
gate, respawn cap ≤3); these are the runtime's own belt-and-suspenders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(env: dict, key: str, default: int) -> int:
    try:
        v = int(env.get(key, "").strip())
        return v if v > 0 else default
    except (ValueError, AttributeError):
        return default


@dataclass(frozen=True)
class OrchConfig:
    """Caps that bound an orchestrator's blast radius."""

    # Total children an orch task may ever have in its subtree.
    max_children: int = 16
    # Subtree depth (root = 0). Children are workers (depth 1) by
    # construction today; this guards future sub-orchestrators.
    max_depth: int = 3
    # Children one turn may create — a per-turn spawn rate-limit.
    max_spawns_per_turn: int = 8
    # Hard ceiling on brain turns for one orchestration (anti-loop).
    max_turns: int = 50

    @classmethod
    def from_env(cls, env: dict | None = None) -> "OrchConfig":
        env = env if env is not None else dict(os.environ)
        return cls(
            max_children=_env_int(env, "KREWCLI_ORCH_MAX_CHILDREN", 16),
            max_depth=_env_int(env, "KREWCLI_ORCH_MAX_DEPTH", 3),
            max_spawns_per_turn=_env_int(env, "KREWCLI_ORCH_MAX_SPAWNS_PER_TURN", 8),
            max_turns=_env_int(env, "KREWCLI_ORCH_MAX_TURNS", 50),
        )

    def spawn_budget(self, current_children: int, depth: int = 0) -> int:
        """Children the brain may spawn THIS turn.

        Zero once the subtree hits ``max_children`` OR the task is already
        at ``max_depth`` (its children would exceed it) — the two
        blast-radius caps that bound an injection/loop-driven runaway."""
        if depth >= self.max_depth:
            return 0
        remaining = max(0, self.max_children - current_children)
        return min(self.max_spawns_per_turn, remaining)
