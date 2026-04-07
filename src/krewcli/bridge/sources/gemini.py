"""Gemini CLI hook payload normalizer.

Gemini's hook payload doesn't natively match the canonical shape,
which is why vibe-island ships a Python adapter
(`vibe-island-gemini-hook.py`) for it. This normalizer covers the
common fields; richer mapping requires the gemini hook contract
to stabilize upstream.
"""

from __future__ import annotations

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    canonicalize_tool_name,
)
from krewcli.bridge.env_collector import collect_env, detect_tty


def normalize(hook_event_name: str, payload: dict) -> CanonicalHookEvent:
    tool_name = canonicalize_tool_name(payload.get("tool_name") or payload.get("tool", ""))
    tool_input = payload.get("tool_input") or payload.get("args") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"raw": tool_input}

    return CanonicalHookEvent(
        hook_event_name=hook_event_name,
        source="gemini",
        session_id=str(payload.get("session_id") or payload.get("conversation_id") or ""),
        cwd=str(payload.get("cwd") or ""),
        tool_name=tool_name,
        tool_input=tool_input,
        prompt=str(payload.get("prompt") or ""),
        env=collect_env(),
        tty=detect_tty(),
    )
