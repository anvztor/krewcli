"""Harness — stateless task execution pipeline.

Connects a Backend to a Session, forming the complete "Brain" from
Anthropic's Managed Agents architecture. The harness is stateless:
all durable state lives in the Session (krewhub event log).

Pipeline:
  1. Setup execution environment (workdir, .agent_context/)
  2. Pre-execution sandbox validation (workdir safety, credential isolation)
  3. Pin session early (crash resilience)
  4. Set task status → working
  5. Execute backend (streaming messages → session)
  6. Report usage
  7. Post completion milestone
  8. Post-execution sandbox validation (secret exfiltration, file boundaries)
  9. Flush session, set final status
  10. Teardown environment
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from krewcli.backend.protocol import Backend, BackendResult
from krewcli.daemon.session import Session
from krewcli.daemon.execenv import ExecutionEnvironment
from krewcli.daemon.sandbox_validator import SandboxValidator

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Maximum wall-clock time for a single backend execution (30 min).
_EXECUTION_TIMEOUT_SECONDS = 1800

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
        self._validator = SandboxValidator()

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

        # Auth track A2 — when the task is bound to an e2b sandbox,
        # signal the attachment so cookrew-beta's task-live-card can
        # update its status to running. Real e2b SDK execution will
        # land in a follow-up; for now this is a metadata-only beacon.
        # We use getattr so legacy ExecutionEnvironment fakes in tests
        # without sandbox_id continue to work.
        sandbox_id = getattr(execenv, "sandbox_id", None)
        if sandbox_id:
            try:
                await session.append(
                    "milestone",
                    body=f"Attached to sandbox {sandbox_id}",
                    payload={
                        "kind": "sandbox.attached",
                        "sandbox_id": sandbox_id,
                    },
                )
            except Exception:
                logger.warning(
                    "harness: failed to emit sandbox.attached for task %s",
                    task_id,
                )

        # 2. Pre-execution sandbox validation
        env_overlay = execenv.build_env(recipe_id=recipe_id)
        pre_check = self._validator.validate_pre_execution(
            working_dir=workdir,
            env=env_overlay,
        )
        if not pre_check.is_valid:
            logger.error(
                "harness: sandbox pre-check failed for task %s:\n%s",
                task_id, pre_check.summary(),
            )
            await execenv.teardown()
            return HarnessResult(
                success=False,
                summary=f"Sandbox validation failed: {pre_check.summary()}",
            )

        # 3. Pin session early for crash resilience
        session_id = f"{backend.name}-{uuid.uuid4().hex[:8]}"
        await session.pin(session_id, workdir)

        # 4. Set task status → working
        try:
            await self._client.update_task_status(task_id, "working")
        except Exception:
            logger.warning("harness: failed to set task %s to working", task_id)

        # 5. Execute backend (with wall-clock timeout)
        backend_session = await backend.execute(prompt, workdir, env=env_overlay)

        cancelled = False
        streamed_texts: list[str] = []
        try:
            async with asyncio.timeout(_EXECUTION_TIMEOUT_SECONDS):
                # Stream messages → session
                async for msg in backend_session.messages_iter():
                    await session.append_from_backend(msg)

                    # Collect streamed text for post-execution secret scan
                    if msg.kind == "agent_reply":
                        text = msg.payload.get("text", msg.body)
                        if text:
                            streamed_texts.append(text)

                    # Periodically check cancellation.
                    if msg.kind in ("session_start", "tool_use"):
                        if await session.check_cancelled():
                            cancelled = True
                            logger.info(
                                "harness: task %s cancelled during execution",
                                task_id,
                            )
                            break

                # 5b. Get terminal result
                result: BackendResult = await backend_session.result()

        except TimeoutError:
            logger.error(
                "harness: task %s timed out after %ds",
                task_id, _EXECUTION_TIMEOUT_SECONDS,
            )
            result = BackendResult(
                success=False,
                summary=f"Task timed out after {_EXECUTION_TIMEOUT_SECONDS}s",
                blocked_reason="execution_timeout",
            )
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

        # 8. Post-execution sandbox validation
        #    Scan both the summary and streamed agent output for secrets.
        if result.success and not cancelled:
            combined_output = "\n".join(
                [result.summary or ""] + streamed_texts,
            )
            post_check = self._validator.validate_post_execution(
                output=combined_output,
                files_modified=result.files_modified,
                working_dir=workdir,
            )
            if not post_check.is_valid:
                logger.warning(
                    "harness: sandbox post-check flagged task %s:\n%s",
                    task_id, post_check.summary(),
                )
                if post_check.has_critical:
                    result = BackendResult(
                        success=False,
                        summary=f"Sandbox post-check failed: {post_check.summary()}",
                        blocked_reason="sandbox_violation",
                        files_modified=result.files_modified,
                        facts=result.facts,
                        code_refs=result.code_refs,
                        usage=result.usage,
                    )

        # 9. Flush session (drain remaining events)
        await session.flush()

        # 10. Set final task status
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

        # 11. Teardown
        await execenv.teardown()

        return HarnessResult(
            success=result.success and not cancelled,
            summary=result.summary,
            files_modified=result.files_modified,
            code_refs=result.code_refs,
            cancelled=cancelled,
        )
