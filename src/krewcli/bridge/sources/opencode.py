"""OpenCode hook payload normalizer.

OpenCode's plugin (`opencode_plugin.js`, ported from vibe-island)
already emits events in the canonical shape via HTTP — but if a
user wires opencode to invoke `krewcli bridge --source opencode`
through stdin, this normalizer handles that case too.

The plugin path is preferred (richer fields, async permission
plumbing); the stdin path is for fallback / debugging.
"""

from __future__ import annotations

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    canonicalize_tool_name,
)
from krewcli.bridge.env_collector import collect_env, detect_tty


def normalize(hook_event_name: str, payload: dict) -> CanonicalHookEvent:
    tool_name = canonicalize_tool_name(payload.get("tool_name") or payload.get("tool", ""))
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"raw": tool_input}

    sid = payload.get("session_id") or payload.get("sessionID") or ""
    if sid and not str(sid).startswith("opencode-"):
        sid = f"opencode-{sid}"

    return CanonicalHookEvent(
        hook_event_name=hook_event_name,
        source="opencode",
        session_id=str(sid),
        cwd=str(payload.get("cwd") or ""),
        tool_name=tool_name,
        tool_input=tool_input,
        prompt=str(payload.get("prompt") or ""),
        last_assistant_message=str(payload.get("last_assistant_message") or ""),
        env=collect_env(),
        tty=detect_tty(),
        extra={"_opencode_request_id": payload.get("_opencode_request_id")},
    )
