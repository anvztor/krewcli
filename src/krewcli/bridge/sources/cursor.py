"""Cursor agent hook payload normalizer.

Cursor's native hook event names from vibe-island's bridge strings:

  beforeShellExecution, afterShellExecution
  beforeMCPExecution, afterMCPExecution
  afterAgentResponse, beforeSubmitPrompt
  afterAgentThought, postToolUseFailure

Cursor is IDE-coupled and we don't typically spawn it via
SpawnManager — this normalizer exists so the bridge can still
ingest events from a user-installed Cursor hook config.
"""

from __future__ import annotations

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    canonicalize_tool_name,
)
from krewcli.bridge.env_collector import collect_env, detect_tty

_CURSOR_EVENT_MAP = {
    "beforeShellExecution": "PreToolUse",
    "afterShellExecution": "PostToolUse",
    "beforeMCPExecution": "PreToolUse",
    "afterMCPExecution": "PostToolUse",
    "afterAgentResponse": "Stop",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "afterAgentThought": "Notification",
    "postToolUseFailure": "PostToolUseFailure",
}


def normalize(hook_event_name: str, payload: dict) -> CanonicalHookEvent:
    canonical_name = _CURSOR_EVENT_MAP.get(hook_event_name, hook_event_name)

    # Cursor packs the command into different keys depending on the event.
    cmd = payload.get("command") or payload.get("shell_command") or ""
    tool_input: dict = {}
    if cmd:
        tool_input["command"] = cmd
    if payload.get("file_path"):
        tool_input["file_path"] = payload["file_path"]

    return CanonicalHookEvent(
        hook_event_name=canonical_name,
        source="cursor",
        session_id=str(payload.get("session_id") or payload.get("conversation_id") or ""),
        cwd=str(payload.get("cwd") or ""),
        tool_name=canonicalize_tool_name(payload.get("tool_name") or "Bash" if cmd else ""),
        tool_input=tool_input,
        prompt=str(payload.get("prompt") or ""),
        env=collect_env(),
        tty=detect_tty(),
        extra={"_cursor_needs_approval": payload.get("_cursor_needs_approval")},
    )
