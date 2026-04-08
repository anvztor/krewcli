"""Codex CLI hook payload normalizer.

Codex hooks are gated behind `[features] codex_hooks = true` in
`~/.codex/config.toml`. Payload field names from vibe-island's
bridge strings:

  codex_event_type, codex_transcript_path, codex_permission_mode,
  codex_session_start_source, codex_last_assistant_message

Mapping is best-effort until codex stabilizes its hook contract.
"""

from __future__ import annotations

from krewcli.bridge.canonical import (
    CanonicalHookEvent,
    canonicalize_tool_name,
)
from krewcli.bridge.env_collector import collect_env, detect_tty

# Codex fires hook events using the same canonical names as Claude
# (verified against a real vibe-island-installed ~/.codex/hooks.json).
# Keep the pass-through map for any codex-internal variants that
# might show up in the future.
_CODEX_EVENT_MAP: dict[str, str] = {}


def normalize(hook_event_name: str, payload: dict) -> CanonicalHookEvent:
    canonical_name = _CODEX_EVENT_MAP.get(hook_event_name, hook_event_name)
    tool_name = canonicalize_tool_name(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"raw": tool_input}

    return CanonicalHookEvent(
        hook_event_name=canonical_name,
        source="codex",
        session_id=str(payload.get("codex_thread_id") or payload.get("session_id") or ""),
        cwd=str(payload.get("cwd") or ""),
        tool_name=tool_name,
        tool_input=tool_input,
        prompt=str(payload.get("prompt") or ""),
        last_assistant_message=str(
            payload.get("codex_last_assistant_message") or ""
        ),
        env=collect_env(),
        tty=detect_tty(),
        extra={
            "codex_event_type": payload.get("codex_event_type"),
            "codex_transcript_path": payload.get("codex_transcript_path"),
            "codex_permission_mode": payload.get("codex_permission_mode"),
            "codex_session_start_source": payload.get("codex_session_start_source"),
            "codex_turn_id": payload.get("codex_turn_id"),
            # Pass through `_codex_call_id` when the upstream payload
            # already carries it. Rollout-watcher sourced events fill
            # this in unconditionally; the normalizer doesn't, so any
            # tooling that POSTs synthetic codex hook events directly
            # must include it for server-side dedup to work.
            "_codex_call_id": payload.get("_codex_call_id"),
        },
    )
