"""Graph code generator — dispatches codegen as a krewhub task to online agents.

The orchestrator creates a temporary "planning" bundle with a single codegen task.
The task description contains the full GraphBuilder API prompt. An online agent
(claude, codex, etc.) picks it up, generates code, and reports back via callback.
The orchestrator polls for completion and reads the full output from events.

No local LLM. No pydantic-ai Agent. The orchestrator is model-agnostic.
"""

from __future__ import annotations

import logging

from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflows.agent_dispatch import (
    dispatch_to_agent,
    pick_available_agent,
    wait_for_task_completion,
)

logger = logging.getLogger(__name__)

CODEGEN_PROMPT = """\
You are a workflow planner. Given a user request, generate Python code using \
pydantic-graph's beta GraphBuilder API to decompose the request into executable steps.

## API Reference

```python
from pydantic_graph.beta import GraphBuilder, StepContext
from pydantic_graph.beta.join import reduce_list_append

g = GraphBuilder(
    state_type=OrchestratorState,
    deps_type=OrchestratorDeps,
    output_type=str,
)

# Define steps — each MUST call dispatch_and_wait(ctx, "step_name")
@g.step
async def step_name(ctx: StepContext[OrchestratorState, OrchestratorDeps, None]) -> str:
    return await dispatch_and_wait(ctx, "step_name")

# Sequential edges
g.add(
    g.edge_from(g.start_node).to(step_a),
    g.edge_from(step_a).to(step_b),
    g.edge_from(step_b).to(g.end_node),
)

# Parallel fork: broadcast sends same input to multiple steps
# Then join collects results
collect = g.join(reduce_list_append, initial_factory=list[str])
g.add(
    g.edge_from(step_a).broadcast().to(step_b, step_c),
    g.edge_from(step_b).to(collect),
    g.edge_from(step_c).to(collect),
    g.edge_from(collect).to(step_d),
)
```

## Rules
1. ALWAYS define `graph = g.build()` as the last line
2. Step names must be valid Python identifiers (snake_case)
3. Every step function body is: `return await dispatch_and_wait(ctx, "step_name")`
4. Use broadcast() for parallel work, join() to collect
5. Output ONLY valid Python code — no markdown fences, no explanations
6. Use 3-7 steps. Don't over-decompose.
7. Common patterns:
   - Feature: scope → implement + write_tests (parallel) → integrate → review
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
    """Strip markdown fences and whitespace from LLM output."""
    code = code.strip()
    if code.startswith("```python"):
        code = code[len("```python"):]
    elif code.startswith("```"):
        code = code[3:]
    if code.endswith("```"):
        code = code[:-3]
    return code.strip()
