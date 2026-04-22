"""Harness — stateless task execution pipeline.

Connects a Backend to a Session, forming the complete "Brain" from
Anthropic's Managed Agents architecture. The harness is stateless:
all durable state lives in the Session (krewhub event log).

Pipeline:
  1. Setup execution environment (workdir, .agent_context/)
  2. Pin session early (crash resilience)
  3. Set task status → working
  4. Execute backend (streaming messages → session)
  5. On completion: report usage, set done/blocked
  6. Teardown environment
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from krewcli.backend.protocol import Backend, BackendResult
from krewcli.daemon.session import Session
from krewcli.daemon.execenv import ExecutionEnvironment

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class HarnessResult:
    """Result from a harness execution."""
    success: bool
    summary: str
    files_modified: list[str] = field(default_factory=list)
    code_refs: list[dict] = field(default_factory=list)
    cancelled: bool = False


class Harness:
    """Stateless task execution harness.

    Can crash and resume: all state lives in the Session (krewhub).
    """

    def __init__(self, client: "KrewHubClient") -> None:
        self._client = client

    async def execute(
        self,
        backend: Backend,
        session: Session,
        execenv: ExecutionEnvironment,
        prompt: str,
        *,
        task_id: str,
        task_title: str = "",
        task_description: str = "",
        recipe_id: str = "",
        bundle_id: str = "",
    ) -> HarnessResult:
        """Execute a task through the full managed agent lifecycle."""
        # 1. Setup execution environment
        workdir = await execenv.setup(
            task_title=task_title,
            task_description=task_description,
            prompt=prompt,
        )

        # 2. Pin session early for crash resilience
        session_id = f"{backend.name}-{uuid.uuid4().hex[:8]}"
        await session.pin(session_id, workdir)

        # 3. Set task status → working
        try:
            await self._client.update_task_status(task_id, "working")
        except Exception:
            logger.warning("harness: failed to set task %s to working", task_id)

        # 4. Execute backend
        env_overlay = execenv.build_env(recipe_id=recipe_id)
        backend_session = await backend.execute(prompt, workdir, env=env_overlay)

        cancelled = False
        try:
            # Stream messages → session
            async for msg in backend_session.messages_iter():
                await session.append_from_backend(msg)

                # Periodically check cancellation.
                # The messages_iter yields fast enough that we don't need
                # a separate timer — just check on session_start and
                # tool_use events (less frequent than agent_reply).
                if msg.kind in ("session_start", "tool_use"):
                    if await session.check_cancelled():
                        cancelled = True
                        logger.info(
                            "harness: task %s cancelled during execution", task_id,
                        )
                        break

            # 5. Get terminal result
            result: BackendResult = await backend_session.result()

        except Exception:
            logger.exception("harness: backend execution failed for task %s", task_id)
            result = BackendResult(
                success=False,
                summary="Backend execution failed unexpectedly",
                blocked_reason="Backend execution error",
            )

        # 6. Report usage
        if result.usage:
            await session.report_usage(result.usage)

        # 7. Post completion milestone with facts and code_refs
        if result.success and not cancelled:
            await session.append(
                "milestone",
                body=result.summary[:256],
                facts=[{"claim": f} if isinstance(f, str) else f for f in result.facts],
                code_refs=result.code_refs,
            )

        # 8. Flush session (drain remaining events)
        await session.flush()

        # 9. Set final task status
        if cancelled:
            # Task was cancelled — status already set by krewhub
            pass
        elif result.success:
            try:
                await self._client.update_task_status(task_id, "done")
            except Exception:
                logger.warning("harness: failed to set task %s to done", task_id)
        else:
            try:
                await self._client.update_task_status(
                    task_id, "blocked",
                    blocked_reason=result.blocked_reason or result.summary,
                )
            except Exception:
                logger.warning("harness: failed to set task %s to blocked", task_id)

        # 10. Teardown
        await execenv.teardown()

        return HarnessResult(
            success=result.success and not cancelled,
            summary=result.summary,
            files_modified=result.files_modified,
            code_refs=result.code_refs,
            cancelled=cancelled,
        )
