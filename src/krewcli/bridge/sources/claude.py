"""Claude Code hook payload normalizer.

Claude's hook stdin shape is the de-facto canonical one — most of
the field names already match. We only need to canonicalize the
tool name and stamp the source.
"""

from __future__ import annotations

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    canonicalize_tool_name,
)
from krewcli.bridge.env_collector import collect_env, detect_tty


def normalize(hook_event_name: str, payload: dict) -> CanonicalHookEvent:
    tool_name = canonicalize_tool_name(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"raw": tool_input}

    extra: dict = {"_raw_tool_name": payload.get("tool_name", "")}

    # Claude's PostToolUse hook includes the tool result under
    # `tool_response`. Mirror codex's `_output_preview` so the
    # workspace event feed can render output regardless of source.
    tool_response = payload.get("tool_response")
    if tool_response is not None:
        if isinstance(tool_response, str):
            preview = tool_response
        else:
            try:
                import json as _json
                preview = _json.dumps(tool_response, ensure_ascii=False)
            except (TypeError, ValueError):
                preview = str(tool_response)
        extra["_output_preview"] = preview[:400]

    return CanonicalHookEvent(
        hook_event_name=hook_event_name,
        source="claude",
        session_id=str(payload.get("session_id") or ""),
        cwd=str(payload.get("cwd") or ""),
        tool_name=tool_name,
        tool_input=tool_input,
        prompt=str(payload.get("prompt") or ""),
        last_assistant_message=str(payload.get("last_assistant_message") or ""),
        env=collect_env(),
        ppid=payload.get("_ppid") or None,
        tty=detect_tty(),
        extra=extra,
    )
