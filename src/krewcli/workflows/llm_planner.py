"""Graph code generator — dispatches codegen as a krewhub task to online agents.

The orchestrator creates a temporary "planning" bundle with a single codegen task.
The task description contains the full GraphBuilder API prompt. An online agent
(claude, codex, etc.) picks it up, generates code, and reports back via callback.
The orchestrator polls for completion and reads the full output from events.

No local LLM. No pydantic-ai Agent. The orchestrator is model-agnostic.
"""

from __future__ import annotations

import logging
import re

from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflows.agent_dispatch import (
    dispatch_to_agent,
    pick_available_agent,
    wait_for_task_completion,
)

logger = logging.getLogger(__name__)

# Matches a fenced code block with optional language tag, capturing the
# body. DOTALL so the body can span multiple lines. Non-greedy so a
# response with multiple fences yields each block separately.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\s*\n(.*?)```", re.DOTALL)

CODEGEN_PROMPT = """\
You are a workflow planner. Given a user request, generate Python code using \
pydantic-graph's beta GraphBuilder API to decompose the request into executable steps.

The code you produce will be executed by krewhub's sandbox. The sandbox \
injects a strict allowlist into the namespace — anything else (imports, \
classes, try/except, lambdas, attribute access starting with `_`, eval, \
open, getattr, etc.) will be rejected at validation time. Stick to the \
template below verbatim.

## Allowed names in the sandbox namespace
GraphBuilder, StepContext, reduce_list_append, dispatch_cycle,
OrchestratorState, OrchestratorDeps, plus the locals `g`, `graph`, `ctx`,
plus standard literal types (str, int, list, dict, etc.) and basic
builtins (len, range, isinstance, ...).

## Required template

```python
g = GraphBuilder(
    state_type=OrchestratorState,
    deps_type=OrchestratorDeps,
    output_type=str,
)

@g.step
async def step_name(ctx: StepContext[OrchestratorState, OrchestratorDeps, None]) -> str:
    return await dispatch_cycle(
        ctx,
        node_id="step_name",
        task_kind="coder",         # planner | coder | reviewer | tester
        instruction="What this step should accomplish",
        max_iterations=2,
    )

g.add(
    g.edge_from(g.start_node).to(step_a),
    g.edge_from(step_a).to(step_b),
    g.edge_from(step_b).to(g.end_node),
)

graph = g.build()
```

For parallel branches, fan out and join back via additional g.edge_from(...) calls:

```python
g.add(
    g.edge_from(step_a).to(step_b),
    g.edge_from(step_a).to(step_c),
    g.edge_from(step_b).to(step_d),
    g.edge_from(step_c).to(step_d),
)
```

Krewhub enforces fanin at runtime: when multiple edges converge on a
step (like `step_d` above), krewhub records those upstream steps as
dependencies on the task row and holds dispatch until every upstream
task reaches DONE. You do NOT need to add explicit Join nodes; just
declare the edges and krewhub honors them. Any upstream that ends in a
non-DONE terminal state (blocked/cancelled) causes the downstream step
to fail fast with `error: upstream failure: ...`.

## Rules
1. ALWAYS define `graph = g.build()` as the last line.
2. Step function names must be valid Python identifiers in snake_case and \
   must match the `node_id=` argument inside the function body exactly.
3. Every step body MUST be a single `return await dispatch_cycle(...)` call \
   with keyword arguments. No other statements inside step bodies.
4. `task_kind` must be one of: planner, coder, reviewer, tester. Pick the \
   one whose role best fits the step.
5. `instruction` is a short human-readable sentence telling the worker \
   agent what this step should accomplish.
6. `max_iterations` must be an integer between 1 and 5.
7. Use 3-7 steps. Don't over-decompose.
8. NEVER use: import, class, try/except, raise, with, lambda, global, \
   eval, exec, open, getattr, setattr, type, super, or any name starting \
   with an underscore. The sandbox will reject the code immediately.
9. Output ONLY the Python code — no markdown fences, no explanations.

## Common patterns
- Feature: scope → implement → write_tests → review
- Bugfix: diagnose → write_failing_test → fix → verify
- Refactor: analyze → plan → implement → update_tests → document

## User Request
{prompt}

## Available Agents
{agents}

Generate the GraphBuilder Python code for this workflow. Output ONLY the Python code.
"""


async def generate_graph_code(
    prompt: str,
    agents: list[dict],
    agent_endpoints: dict[str, str],
    krewhub_client: KrewHubClient | None = None,
    recipe_id: str = "",
) -> str | None:
    """Generate GraphBuilder code by dispatching a codegen task to an online agent.

    Flow:
    1. Create a "planning" bundle in krewhub with a single codegen task
    2. Dispatch the task to an online agent via A2A message/send
    3. Poll krewhub until the agent completes it
    4. Read the full agent output from the task's events
    5. Extract and return the graph code

    Returns the Python code string, or None if generation failed.
    """
    if not agent_endpoints:
        logger.warning("No agent endpoints available for codegen")
        return None

    agent_summary = ", ".join(
        a.get("display_name", a.get("agent_id", "unknown"))
        for a in agents
    )
    codegen_prompt = CODEGEN_PROMPT.format(prompt=prompt, agents=agent_summary)

    # If we have krewhub access, use the task dispatch flow (async, full output)
    if krewhub_client and recipe_id:
        return await _generate_via_krewhub_task(
            codegen_prompt, krewhub_client, recipe_id, agent_endpoints,
        )

    # Fallback: try direct A2A (works with sync agents like in tests)
    return await _generate_via_direct_a2a(codegen_prompt, agent_endpoints)


async def _generate_via_krewhub_task(
    codegen_prompt: str,
    krewhub_client: KrewHubClient,
    recipe_id: str,
    agent_endpoints: dict[str, str],
) -> str | None:
    """Create a krewhub task for codegen, dispatch to agent, poll for result."""
    import httpx

    # 1. Create a planning bundle with a single codegen task
    bundle, tasks = await krewhub_client.create_bundle(
        recipe_id=recipe_id,
        prompt="[orchestrator:codegen] Generate workflow graph",
        requested_by="orchestrator",
        tasks=[{
            "title": "Generate pydantic-graph workflow code",
            "description": codegen_prompt,
        }],
    )
    bundle_id = bundle["id"]
    task_id = tasks[0]["id"]
    logger.info("Created codegen bundle %s, task %s", bundle_id, task_id)

    # 2. Dispatch the task directly to an online agent
    agent_id, endpoint_url = pick_available_agent(agent_endpoints)
    logger.info("Dispatching codegen task to agent %s", agent_id)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as a2a_client:
        dispatched = await dispatch_to_agent(
            a2a_client, endpoint_url, task_id, bundle_id,
            codegen_prompt,
            recipe_meta={},
        )

    if not dispatched:
        logger.warning("Agent %s rejected codegen task", agent_id)
        return None

    # 3. Poll until the agent completes
    try:
        completed_task = await wait_for_task_completion(
            krewhub_client, task_id,
            poll_interval=2.0,
            timeout=120.0,
        )
    except TimeoutError:
        logger.warning("Codegen task %s timed out", task_id)
        return None

    if completed_task.get("status") != "done":
        logger.warning("Codegen task %s ended with status %s", task_id, completed_task.get("status"))
        return None

    # 4. Read the full output from bundle events
    events = await krewhub_client.get_bundle_events(bundle_id)
    for event in events:
        if event.get("task_id") == task_id and event.get("type") == "milestone":
            body = event.get("body", "")
            if body:
                code = _clean_code(body)
                if "g.build()" in code or "graph = " in code:
                    logger.info("Extracted graph code from codegen task event")
                    return code

    logger.warning("Codegen task completed but no graph code found in events")
    return None


async def _generate_via_direct_a2a(
    codegen_prompt: str,
    agent_endpoints: dict[str, str],
) -> str | None:
    """Fallback: send codegen prompt directly via A2A and try to read from response.

    Works with synchronous agents (e.g. test fakes) that return artifacts immediately.
    """
    import uuid
    import httpx

    for agent_id, endpoint_url in agent_endpoints.items():
        logger.info("Requesting graph code from agent %s (direct A2A)", agent_id)
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "user",
                    "parts": [{"kind": "text", "text": codegen_prompt}],
                    "metadata": {"task_type": "codegen_graph"},
                },
            },
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(endpoint_url, json=payload)
                if resp.status_code != 200:
                    continue

                body = resp.json()
                result = body.get("result", {})

                # Try artifacts first (sync agents)
                for artifact in result.get("artifacts", []):
                    for part in artifact.get("parts", []):
                        if part.get("kind") == "text":
                            code = _clean_code(part["text"])
                            if "g.build()" in code or "graph = " in code:
                                return code

                # Try status message
                status = result.get("status", {})
                msg = status.get("message", {})
                for part in msg.get("parts", []):
                    if isinstance(part, dict) and part.get("kind") == "text":
                        text = part.get("text", "")
                        code = _clean_code(text)
                        if "g.build()" in code or "graph = " in code:
                            return code

        except (httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning("Agent at %s unreachable: %s", endpoint_url, exc)

    return None


def _clean_code(code: str) -> str:
    """Extract graph source from LLM output, tolerating common wrappers.

    LLMs routinely wrap code in markdown fences, prepend prose like
    "Here's the workflow:", or reflexively add ``from __future__ import
    annotations`` at the top. The krewhub sandbox validator rejects any
    of those constructs (ImportFrom nodes, non-code leading text), so
    this helper strips them before we POST the code to the attach-graph
    endpoint.

    Strategy:
        1. If the response contains any ```-fenced blocks, pick the
           first one whose body mentions ``g.build()`` or ``graph =``;
           otherwise take the first block. This handles the "here's
           your code:\\n```python...```\\nhope that helps!" shape.
        2. If no fenced blocks exist, treat the whole response as code.
        3. Strip ``from __future__ import ...`` lines — common LLM
           reflex that the sandbox's ImportFrom check rejects.
        4. Trim surrounding whitespace.

    Pure function; no side effects.
    """
    if not code:
        return ""

    # 1. Prefer fenced block content when fences are present.
    fenced_blocks = _FENCE_RE.findall(code)
    if fenced_blocks:
        preferred = next(
            (b for b in fenced_blocks if "g.build" in b or "graph = " in b or "graph=" in b),
            fenced_blocks[0],
        )
        code = preferred

    code = code.strip()

    # 2. Strip any `from __future__ import ...` lines — the sandbox
    #    rejects every ImportFrom node but LLMs reflexively prepend
    #    `from __future__ import annotations`.
    code = re.sub(
        r"^\s*from\s+__future__\s+import\s+[^\n]+\n",
        "",
        code,
        flags=re.MULTILINE,
    )

    return code.strip()
