"""Tier 2 Agent: pydantic-ai framework with tools.

Full tool-use loop in-process. The LLM calls tools (bash, file, git),
gets results, calls more tools, until it produces a structured TaskResult.
"""

from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.artifact import new_text_artifact
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from pydantic_ai import Agent

from krewcli.agents.models import TaskResult
from krewcli.a2a.tools.bash_tool import TaskDeps, bash_exec
from krewcli.a2a.tools.file_tools import read_file, write_file, edit_file
from krewcli.a2a.tools.git_tools import git_diff, git_status

SYSTEM_PROMPT = """\
You are a coding agent executing a task in a git repository.
You have tools to read/write files, run bash commands, and check git status.

Work step by step:
1. Understand the task
2. Explore the codebase (read files, check structure)
3. Make changes (write/edit files)
4. Verify your work (run tests, check git diff)
5. Return a structured result

Be precise. Make minimal changes. Run tests after editing code.
"""


class FrameworkExecutor(AgentExecutor):
    """Tier 2: pydantic-ai Agent with tools. Full tool-use loop."""

    def __init__(self, model: str, working_dir: str) -> None:
        self._model = model
        self._working_dir = working_dir
        self._agent = Agent(
            model,
            result_type=TaskResult,
            system_prompt=SYSTEM_PROMPT,
            deps_type=TaskDeps,
        )
        self._agent.tool(bash_exec)
        self._agent.tool(read_file)
        self._agent.tool(write_file)
        self._agent.tool(edit_file)
        self._agent.tool(git_diff)
        self._agent.tool(git_status)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=False, status=TaskStatus(
                state=TaskState.working,
                message=new_agent_text_message(f"Framework agent executing with {self._model}..."),
            ),
        ))

        prompt = _extract_text(context)
        deps = TaskDeps(working_dir=self._working_dir)

        try:
            result = await self._agent.run(prompt, deps=deps)
            task_result: TaskResult = result.output

            await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                artifact=new_text_artifact(
                    name="result",
                    text=json.dumps(task_result.model_dump(), indent=2),
                ),
            ))
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                final=True, status=TaskStatus(state=TaskState.completed),
            ))

        except Exception as exc:
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                final=True, status=TaskStatus(
                    state=TaskState.failed,
                    message=new_agent_text_message(f"Framework agent failed: {exc}"),
                ),
            ))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=True, status=TaskStatus(state=TaskState.canceled),
        ))


def build_framework_card(provider: str, host: str, port: int) -> AgentCard:
    return AgentCard(
        name=f"framework:{provider}",
        description=f"pydantic-ai coding agent with tools (bash, file, git) via {provider}. "
        "Stateful: reads/writes files, runs commands, modifies git state.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")],
        skills=[AgentSkill(
            id=f"code:{provider}",
            name=f"Framework Agent ({provider})",
            description="Stateful coding agent with bash, file, and git tools.",
            tags=["code", "implement", "fix", "test", "refactor"],
            examples=[
                "Implement a REST API endpoint for user registration",
                "Fix the failing tests in the auth module",
            ],
        )],
    )


def _extract_text(context: RequestContext) -> str:
    if context.message and context.message.parts:
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                return part.root.text
            if hasattr(part, "text"):
                return part.text
    return ""
