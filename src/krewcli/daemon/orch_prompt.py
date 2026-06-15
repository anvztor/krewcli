"""Prompt construction for the orch-agent's turns (gap 5, C4).

Each orch turn the brain wakes, reads what changed in its subtree, and
decides the next action. This module turns krewhub state into that
prompt:

  * the goal (root task's Brief or title/description),
  * the conversation so far (prior orch turns + operator messages),
  * NEW child reports — the ``subagent_report`` events krewhub's
    OrchController projects onto A's tape when a child completes
    (``_maybe_flow_subagent_report``); this is the Report-up-the-link
    half of the Row-0⇄worker loop the brain consumes,
  * the current subtree state table.

Pure functions over plain dicts so they're trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from krewcli.daemon.orch_subtree import SubtreeView


@dataclass(frozen=True)
class ChildReport:
    """A child's Report as flowed up onto the parent's tape."""

    from_task: str
    link_id: str | None
    report: dict
    seq: int

    def summary(self) -> str:
        r = self.report or {}
        status = r.get("status", "?")
        bits = [f"status={status}"]
        if r.get("prs"):
            bits.append("prs=" + ", ".join(map(str, r["prs"])))
        if r.get("artifacts"):
            bits.append("artifacts=" + ", ".join(map(str, r["artifacts"])))
        if r.get("blockers"):
            bits.append("blockers=" + "; ".join(map(str, r["blockers"])))
        if r.get("decisions_needed"):
            bits.append("decisions_needed=" + "; ".join(map(str, r["decisions_needed"])))
        return " · ".join(bits)


def extract_child_reports(events: list[dict]) -> list[ChildReport]:
    """Pull ``subagent_report`` turns off the parent's event tape.

    krewhub injects these as ``agent_reply`` events with
    ``payload = {kind: "subagent_report", from_task, link_id, report}``
    (orch_controller ``_maybe_flow_subagent_report``). Oldest-first.
    """
    reports: list[ChildReport] = []
    for ev in events:
        if ev.get("type") != "agent_reply":
            continue
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("kind") != "subagent_report":
            continue
        reports.append(
            ChildReport(
                from_task=str(payload.get("from_task", "")),
                link_id=payload.get("link_id"),
                report=payload.get("report") or {},
                seq=int(ev.get("seq", 0) or 0),
            )
        )
    return reports


def extract_orch_turns(
    events: list[dict],
    orch_agent_id: str,
) -> list[tuple[str, str]]:
    """Prior conversation turns for continuity (oldest-first).

    ORCH = the brain's own previous replies; HUMAN = operator messages
    (delegate answers / follow-ups projected onto the tape). Skips
    subagent_report turns — those render separately as Child reports.
    """
    turns: list[tuple[str, str]] = []
    for ev in events:
        if ev.get("type") != "agent_reply":
            continue
        payload = ev.get("payload") or {}
        if isinstance(payload, dict) and payload.get("kind") == "subagent_report":
            continue
        text = ""
        if isinstance(payload, dict):
            text = payload.get("text") or ""
        if not text:
            text = ev.get("body") or ""
        text = text.strip()
        if not text:
            continue
        actor = ev.get("actor_type")
        actor_id = ev.get("actor_id")
        if actor == "human":
            role = "HUMAN"
        elif actor_id == orch_agent_id:
            role = "ORCH"
        else:
            role = "ASSISTANT"
        turns.append((role, text))
    return turns


def _goal_block(root_task: dict) -> str:
    """Render the root goal from its Brief (preferred) or title/description."""
    brief = root_task.get("brief") or root_task.get("brief_json")
    if isinstance(brief, dict) and brief.get("goal"):
        lines = [f"GOAL: {brief['goal']}"]
        if brief.get("deliverable"):
            lines.append(f"DELIVERABLE: {brief['deliverable']}")
        if brief.get("context"):
            lines.append(f"CONTEXT: {brief['context']}")
        if brief.get("constraints"):
            lines.append("CONSTRAINTS:")
            lines += [f"  - {c}" for c in brief["constraints"]]
        return "\n".join(lines)
    title = root_task.get("title", "") or "(untitled)"
    desc = root_task.get("description", "") or ""
    block = f"GOAL: {title}"
    if desc:
        block += f"\n\n{desc}"
    return block


def _subtree_table(subtree: SubtreeView) -> str:
    if not subtree.children:
        return "(no children spawned yet)"
    lines = ["task_id            depth  kind      status"]
    for c in subtree.children:
        lines.append(
            f"{c.task_id[:16]:<18} {c.depth:<5}  {c.kind:<8}  {c.status}"
            + (f'   "{c.title[:40]}"' if c.title else "")
        )
    return "\n".join(lines)


def build_orch_prompt(
    *,
    root_task: dict,
    subtree: SubtreeView,
    new_reports: list[ChildReport],
    prior_turns: list[tuple[str, str]],
    first_turn: bool,
) -> str:
    """Assemble the orchestrator's prompt for one turn."""
    parts: list[str] = []

    if first_turn:
        parts.append(
            "You are starting orchestration of this goal. Decompose it into "
            "subtasks and spawn a worker for each with `spawn_subtask`, then "
            "end your turn."
        )
    else:
        parts.append(
            "You are continuing orchestration. Review what your subtree "
            "reported, decide the next action per child (accept / spawn a "
            "follow-up / escalate to human), then end your turn."
        )

    parts.append("\n## Your goal\n" + _goal_block(root_task))

    if prior_turns:
        parts.append("\n## Conversation so far")
        # Cap to the last ~20 turns to bound context.
        for role, text in prior_turns[-20:]:
            parts.append(f"{role}: {text}")

    if new_reports:
        parts.append("\n## Child reports (NEW since your last turn)")
        for rep in new_reports:
            parts.append(f"- from task {rep.from_task}: {rep.summary()}")
    elif not first_turn:
        parts.append(
            "\n## Child reports\n(no new reports since your last turn)"
        )

    parts.append("\n## Subtree state (current)\n" + _subtree_table(subtree))

    if subtree.all_done():
        parts.append(
            "\nEvery subtask is done. If the goal is satisfied, say so "
            "clearly and do NOT spawn more work."
        )

    return "\n".join(parts)
