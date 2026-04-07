"""Codex rollout JSONL replay.

Codex's hook system only exposes three high-level events
(`SessionStart`, `UserPromptSubmit`, `Stop`). Everything
interesting — tool calls, tool results, reasoning, assistant
messages — lives in the rollout file codex writes under
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl`.

This matches vibe-island's `CodexSessionWatcher` strategy: tail
the rollout file and translate each JSONL item into a canonical
hook event. The difference is we don't run a long-lived watcher;
we read the file at `Stop` time once codex has finished writing
and replay the entire rollout through the bridge forwarder.

Rollout item shape (from a real codex-cli 0.117+ file):

  {
    "timestamp": "...",
    "type": "session_meta" | "event_msg" | "response_item" | "turn_context",
    "payload": { "type": <subtype>, ... }
  }

Subtypes we care about:
  response_item :: function_call          → PreToolUse
  response_item :: function_call_output   → PostToolUse
  response_item :: reasoning              → Notification (thinking block)
  response_item :: message                → Notification (assistant prose)
  event_msg     :: user_message           → UserPromptSubmit
  event_msg     :: task_started           → SessionStart
  event_msg     :: task_complete          → Stop
  event_msg     :: turn_aborted           → StopFailure

Anything else is dropped.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    canonicalize_tool_name,
)
from krewcli.bridge.env_collector import collect_env, detect_tty

logger = logging.getLogger(__name__)


def find_rollout_path(session_id: str, codex_home: str | None = None) -> Path | None:
    """Locate the rollout file for a given codex session id.

    Rollout files live at
    `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl`.
    We walk recently-modified files rather than parsing the date
    partition, which keeps us robust to timezone drift.
    """
    root = Path(codex_home or os.environ.get("CODEX_HOME") or
                (Path.home() / ".codex")) / "sessions"
    if not root.exists():
        return None

    candidates = sorted(
        root.rglob(f"*{session_id}*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def replay_rollout(
    rollout_path: Path,
    *,
    session_id: str,
    cwd: str = "",
) -> list[CanonicalHookEvent]:
    """Parse a rollout file into canonical hook events.

    Returns the events in file order. The caller forwards them
    through `bridge.forwarder.forward()` individually.
    """
    if not rollout_path.exists():
        logger.warning("codex rollout: %s does not exist", rollout_path)
        return []

    events: list[CanonicalHookEvent] = []
    env_snapshot = collect_env()
    tty = detect_tty()
    # Index function_call items by call_id so we can pair outputs
    # back to the tool name (function_call_output only carries call_id).
    call_id_to_tool: dict[str, tuple[str, dict]] = {}

    with rollout_path.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            outer = item.get("type", "")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            inner = payload.get("type", "")

            if outer == "session_meta":
                cwd = payload.get("cwd") or cwd
                continue

            if outer == "event_msg":
                ev = _event_msg_to_canonical(
                    inner, payload, session_id, cwd, env_snapshot, tty,
                )
                if ev is not None:
                    events.append(ev)
                continue

            if outer == "response_item":
                ev = _response_item_to_canonical(
                    inner, payload, session_id, cwd,
                    env_snapshot, tty, call_id_to_tool,
                )
                if ev is not None:
                    events.append(ev)
                continue

    return events


def _event_msg_to_canonical(
    inner: str,
    payload: dict,
    session_id: str,
    cwd: str,
    env_snapshot: dict,
    tty: str | None,
) -> CanonicalHookEvent | None:
    base = {
        "source": "codex",
        "session_id": session_id,
        "cwd": cwd,
        "env": env_snapshot,
        "tty": tty,
    }

    if inner == "task_started":
        return CanonicalHookEvent(
            hook_event_name="SessionStart",
            **base,
            extra={"turn_id": payload.get("turn_id")},
        )
    if inner == "task_complete":
        return CanonicalHookEvent(hook_event_name="Stop", **base)
    if inner == "turn_aborted":
        return CanonicalHookEvent(hook_event_name="StopFailure", **base)
    if inner == "user_message":
        return CanonicalHookEvent(
            hook_event_name="UserPromptSubmit",
            prompt=str(payload.get("message") or "")[:2000],
            **base,
        )
    if inner == "agent_message":
        msg = str(payload.get("message") or "")[:2000]
        return CanonicalHookEvent(
            hook_event_name="Notification",
            last_assistant_message=msg,
            **base,
            extra={"_codex_kind": "agent_message", "phase": payload.get("phase")},
        )
    return None


def _response_item_to_canonical(
    inner: str,
    payload: dict,
    session_id: str,
    cwd: str,
    env_snapshot: dict,
    tty: str | None,
    call_id_to_tool: dict[str, tuple[str, dict]],
) -> CanonicalHookEvent | None:
    base = {
        "source": "codex",
        "session_id": session_id,
        "cwd": cwd,
        "env": env_snapshot,
        "tty": tty,
    }

    if inner == "function_call":
        raw_name = payload.get("name", "") or ""
        tool_name = canonicalize_tool_name(raw_name)
        args_raw = payload.get("arguments", "") or ""
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            args = {"raw": args_raw[:500] if isinstance(args_raw, str) else ""}
        if not isinstance(args, dict):
            args = {"raw": args}

        # Remember (name, args) so function_call_output can pair up.
        call_id = payload.get("call_id") or ""
        if call_id:
            call_id_to_tool[call_id] = (tool_name, args)

        return CanonicalHookEvent(
            hook_event_name="PreToolUse",
            tool_name=tool_name,
            tool_input=_shape_tool_input(raw_name, args),
            **base,
            extra={
                "_codex_call_id": call_id,
                "_raw_tool_name": raw_name,
            },
        )

    if inner == "function_call_output":
        call_id = payload.get("call_id") or ""
        paired = call_id_to_tool.get(call_id, ("", {}))
        tool_name, args = paired
        output = payload.get("output", "")
        if isinstance(output, str):
            output_preview = output[:400]
        else:
            output_preview = str(output)[:400]
        return CanonicalHookEvent(
            hook_event_name="PostToolUse",
            tool_name=tool_name,
            tool_input=_shape_tool_input(tool_name, args),
            **base,
            extra={
                "_codex_call_id": call_id,
                "_output_preview": output_preview,
            },
        )

    if inner == "reasoning":
        # Thinking block — summary is short, content is the raw trace.
        summary_items = payload.get("summary") or []
        summary_text = ""
        if isinstance(summary_items, list) and summary_items:
            first = summary_items[0]
            if isinstance(first, dict):
                summary_text = str(first.get("text") or "")[:500]
        return CanonicalHookEvent(
            hook_event_name="Notification",
            **base,
            extra={
                "_codex_kind": "reasoning",
                "summary": summary_text,
            },
        )

    if inner == "message":
        role = payload.get("role", "")
        content = payload.get("content", [])
        text = _extract_message_text(content)
        if role == "user":
            return CanonicalHookEvent(
                hook_event_name="UserPromptSubmit",
                prompt=text[:2000],
                **base,
            )
        if role == "assistant":
            return CanonicalHookEvent(
                hook_event_name="Notification",
                last_assistant_message=text[:2000],
                **base,
                extra={"_codex_kind": "assistant_message"},
            )
        return None

    return None


def _shape_tool_input(tool_name: str, args: dict) -> dict:
    """Normalize codex's arg shape into canonical Claude-like keys.

    Codex's `exec_command` tool uses `cmd` + `workdir`; canonical
    form is `command` + `cwd`. Same for a few other common tools.
    """
    if not isinstance(args, dict):
        return {}
    out = dict(args)
    if "cmd" in out and "command" not in out:
        out["command"] = out["cmd"]
    if "workdir" in out and "cwd" not in out:
        out["cwd"] = out["workdir"]
    return out


def _extract_message_text(content) -> str:
    """Pull plain text from a codex message content array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for piece in content:
        if isinstance(piece, dict):
            t = piece.get("text") or piece.get("content")
            if isinstance(t, str):
                out.append(t)
    return "\n".join(out)
