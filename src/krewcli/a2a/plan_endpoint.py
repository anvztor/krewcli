"""REST /plan endpoint for the krewcli A2A server.

This endpoint accepts a prompt and returns planned tasks with dependencies
using a real LLM call via pydantic-ai. It's called by krewhub's /api/v1/plan
proxy endpoint, which cookrew calls when creating a bundle.

Flow: cookrew → krewhub /plan → agent /plan → LLM → tasks with deps
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

logger = logging.getLogger(__name__)


class PlannedTask(BaseModel):
    title: str
    description: str = ""
    depends_on: list[int] = Field(default_factory=list)


class TaskPlan(BaseModel):
    tasks: list[PlannedTask]
    reasoning: str = ""


PLAN_SYSTEM_PROMPT = """\
You are a project planner for a software development team.
Given a request, decompose it into 3-6 concrete, actionable coding tasks.

Rules:
- Each task should be independently executable by a coding agent
- Tasks should be ordered by dependency (earlier tasks first)
- Use depends_on to reference earlier task indices (0-based)
- Task 0 should never depend on anything
- Be specific — "Implement user login endpoint" not "Do the work"
- Include a planning/scoping task first and a review/test task last

Return a TaskPlan with the list of tasks and brief reasoning.
"""

_plan_agent: Agent[None, TaskPlan] | None = None


def _get_plan_agent(model: str) -> Agent[None, TaskPlan]:
    global _plan_agent
    if _plan_agent is None:
        llm_model = _build_model(model)
        _plan_agent = Agent(
            llm_model,
            output_type=TaskPlan,
            system_prompt=PLAN_SYSTEM_PROMPT,
        )
    return _plan_agent


def _build_model(model_spec: str):
    """Build a pydantic-ai model, respecting gateway env vars.

    Supports:
    - ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (gateway/proxy)
    - ANTHROPIC_API_KEY (direct)
    - Falls back to pydantic-ai default resolution
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    api_key = (
        os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )

    if model_spec.startswith("anthropic:"):
        model_name = model_spec.split(":", 1)[1]
    else:
        model_name = model_spec

    if base_url or api_key:
        provider = AnthropicProvider(
            api_key=api_key or "dummy",
            base_url=base_url,
        )
        return AnthropicModel(model_name, provider=provider)

    # Default: let pydantic-ai resolve from environment
    return model_spec


async def handle_plan(request: Request) -> JSONResponse:
    """POST /plan — decompose a prompt into tasks with dependencies."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    model = request.app.state.plan_model
    agent = _get_plan_agent(model)

    try:
        result = await agent.run(f"Decompose this request into tasks:\n\n{prompt}")
        plan: TaskPlan = result.output

        tasks = [
            {
                "title": t.title,
                "description": t.description,
                "dependsOn": t.depends_on,
            }
            for t in plan.tasks
        ]

        return JSONResponse({"tasks": tasks, "reasoning": plan.reasoning})

    except Exception as exc:
        logger.exception("LLM planning failed")
        return JSONResponse(
            {"error": f"LLM planning failed: {exc}"},
            status_code=500,
        )


plan_routes = [
    Route("/plan", handle_plan, methods=["POST"]),
]
