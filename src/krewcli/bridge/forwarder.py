"""Forward canonical hook events to krewhub.

Primary destination is `POST /api/v1/tasks/{task_id}/events` — the
existing first-class event API. Falls back to the legacy
`POST /api/v1/hooks/ingest` when no `task_id` is bound (e.g. for
ad-hoc agent runs not dispatched through SpawnManager).

The bridge never blocks the calling agent: every error is logged
to stderr and the call returns silently, mirroring vibe-island's
fire-and-forget socket pattern.
"""

from __future__ import annotations

import os
import sys
from typing import Final

import httpx

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    krewhub_event_type,
)

DEFAULT_TIMEOUT: Final = 5.0


def _build_actor_id(
    event: CanonicalHookEvent,
    env: dict[str, str] | None = None,
) -> str:
    source = env if env is not None else os.environ
    explicit = source.get("KREWHUB_AGENT_ID", "").strip()
    if explicit:
        return explicit
    return f"{event.source or 'agent'}@hook"


def _build_body(event: CanonicalHookEvent) -> str:
    """Short, human-readable summary for the event card."""
    name = event.hook_event_name
    tool = event.tool_name
    inp = event.tool_input or {}

    if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"):
        target = ""
        if isinstance(inp, dict):
            target = (
                inp.get("file_path")
                or inp.get("path")
                or inp.get("command")
                or inp.get("query")
                or inp.get("pattern")
                or inp.get("url")
                or ""
            )
            if isinstance(target, str) and len(target) > 160:
                target = target[:157] + "..."
        suffix = ""
        if name == "PostToolUse":
            suffix = " ✓"
        elif name == "PostToolUseFailure":
            suffix = " ✗"
        return f"{tool}({target}){suffix}" if target else f"{tool}{suffix}"

    if name == "SessionStart":
        sid = (event.session_id or "")[:12]
        return f"session_start session={sid} cwd={event.cwd}"
    if name in ("Stop", "SessionEnd", "SubagentStop", "StopFailure"):
        sid = (event.session_id or "")[:12]
        return f"{name.lower()} session={sid}"
    if name == "UserPromptSubmit":
        return (event.prompt or "")[:200]
    if name == "Notification":
        # Codex (and other sources) emit assistant prose and reasoning
        # as Notification events. Prefer the actual message text so the
        # event card shows something meaningful instead of "Notification".
        msg = (event.last_assistant_message or "").strip()
        if msg:
            return msg
        summary = ""
        extra = event.extra or {}
        if isinstance(extra, dict):
            raw_summary = extra.get("summary")
            if isinstance(raw_summary, str):
                summary = raw_summary.strip()
        if summary:
            return summary
    return name


def forward(
    event: CanonicalHookEvent,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """POST a canonical hook event to krewhub.

    `env` — optional per-call env dict. When set, URLs/auth/task_id
    come from this dict instead of os.environ. This lets the
    in-process codex rollout watcher forward events for a specific
    spawn without leaking state across concurrent spawns.
    """
    source = env if env is not None else os.environ
    krewhub_url = source.get("KREWHUB_URL", "http://127.0.0.1:8420").rstrip("/")
    api_key = source.get("KREWHUB_API_KEY", "dev-api-key")
    task_id = source.get("KREWHUB_TASK_ID", "").strip()
    recipe_id = source.get("KREWHUB_RECIPE_ID", "").strip()
    bundle_id = source.get("KREWHUB_BUNDLE_ID", "").strip()

    payload = event.to_payload()
    body = _build_body(event)
    actor_id = _build_actor_id(event, env=env)
    event_type = krewhub_event_type(event.hook_event_name)

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    if task_id:
        url = f"{krewhub_url}/api/v1/tasks/{task_id}/events"
        body_json = {
            "type": event_type,
            "actor_id": actor_id,
            "actor_type": "hook",
            "body": body[:240],
            "facts": [],
            "code_refs": [],
            "payload": payload,
        }
    else:
        # Fallback for ad-hoc runs without a bound task. Uses the
        # recipe-level ingest endpoint that already understands the
        # canonical hook payload shape.
        url = f"{krewhub_url}/api/v1/hooks/ingest"
        body_json = {
            "hook_event_name": event.hook_event_name,
            "task_id": task_id or None,
            "bundle_id": bundle_id or None,
            "recipe_id": recipe_id or None,
            "agent_id": actor_id,
            "session_id": event.session_id,
            "cwd": event.cwd,
            "payload": payload,
        }

    try:
        resp = httpx.post(
            url, json=body_json, headers=headers, timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            print(
                f"krewcli bridge: {resp.status_code} {resp.text[:200]}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — never block the agent
        print(f"krewcli bridge: {exc}", file=sys.stderr)
