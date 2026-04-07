"""Factory Droid / Amp / Qoder hook payload normalizer.

These all share the Amp plugin model — a TS plugin file at
`~/.config/amp/plugins/vibe-island.ts` listens to internal events
and forwards them. The plugin posts to our bridge HTTP endpoint
(or invokes the CLI shim with stdin); this normalizer handles
the stdin variant.

Field shape is best-effort until the Amp plugin contract
stabilizes upstream.
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

    return CanonicalHookEvent(
        hook_event_name=hook_event_name,
        source="droid",
        session_id=str(payload.get("session_id") or ""),
        cwd=str(payload.get("cwd") or ""),
        tool_name=tool_name,
        tool_input=tool_input,
        prompt=str(payload.get("prompt") or ""),
        env=collect_env(),
        tty=detect_tty(),
    )
