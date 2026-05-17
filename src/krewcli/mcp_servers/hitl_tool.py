"""hitl.request_access — the ONLY credential pathway in Phase 0.

When the brain hits 401/403 / Bad credentials on an upstream API, it
calls this tool. The bridge emits op:auth_required on the elicit
channel. cookrew-beta opens the auth-required-popout; the user clicks
CONNECT; the auth-origin SPA does the OAuth dance; the SPA POSTs the
fresh access_token to krewhub's /credential-relay; krewhub forwards it
to the task's sandbox AND resolves the elicit with action=accept.

From the brain's perspective: hitl.request_access blocks until the
elicit resolves, then returns. On status="granted", credentials are
already in env — the brain just retries.

task_id is derived from server-side bridge context (NOT a tool arg —
codex v3 BLOCKER #6: the agent must not be able to forge task_id to
bypass the per-task grant budget).
"""
from __future__ import annotations
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class HitlTool:
    emitter: Any                              # async emit_elicit(payload, task_id) -> elicit_id
    resolver: Any                             # async wait_for_resolution(task_id, elicit_id) -> dict
    task_id_provider: Callable[[], str]       # server-derived; NEVER from a tool arg
    default_timeout_s: float = 300.0
    max_grants_per_provider: int = 3
    _budget: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    async def request_access(
        self,
        *,
        provider: str,
        reason: str,
        resource: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        # Task identity comes from the bridge — agent cannot supply.
        task_id = self.task_id_provider()

        # Per-task per-provider budget.
        key = (task_id, provider)
        if self._budget[key] >= self.max_grants_per_provider:
            return {
                "status": "denied",
                "reason": "budget_exhausted",
                "retry_hint": (
                    f"reached the cap of {self.max_grants_per_provider} {provider} grants "
                    "for this task; surface the failure to the user instead of retrying"
                ),
            }
        self._budget[key] += 1

        payload: dict[str, Any] = {
            "op": "auth_required",
            "provider": provider,
            "reason": reason,
        }
        if resource:
            payload["resource"] = resource

        elicit_id = await self.emitter.emit_elicit(payload=payload, task_id=task_id)

        try:
            res = await asyncio.wait_for(
                self.resolver.wait_for_resolution(task_id=task_id, elicit_id=elicit_id),
                timeout=timeout_s or self.default_timeout_s,
            )
        except asyncio.TimeoutError:
            return {"status": "timeout", "reason": "user did not respond within window"}

        if res.get("action") == "accept":
            return {
                "status": "granted",
                "retry_hint": (
                    f"{provider.upper()}_TOKEN is now in your env; retry the failed op. "
                    "Use the sandbox tools (gh, curl, git) — env injection landed via "
                    "SandboxHand."
                ),
            }
        return {"status": "denied", "reason": res.get("reason", "user_declined")}
