"""Daemon-side handler for `method="delegate"` invocations.

Counterpart to krewhub's AgentHand. When the SSEWatcher pulls an A2A
invocation with `method="delegate"`, this handler:

1. Reads `params.input` (string or dict; dicts may carry a `message` key).
2. Picks a backend by `agent_name` (falls back to first registered backend).
3. Spawns the brain on the input via `Backend.execute()`.
4. Awaits the result and returns `{text: <reply>}`.

No krewhub task lifecycle, no bundle context — just a one-shot brain
invocation. krewhub's AgentHand takes care of mapping the daemon's
reply into a ResultEnvelope on the operator-visible invocation row.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from krewcli.backend.protocol import Backend


logger = logging.getLogger(__name__)


_REPLY_CAP = 4096


async def handle_delegate_invocation(
    payload: Mapping[str, Any],
    backends: Mapping[str, Backend],
    *,
    working_dir: str,
) -> dict[str, str]:
    """Run the targeted backend against `params.input`; return `{text: reply}`."""
    if not backends:
        return {"text": "no backend available for delegate invocation"}

    params = payload.get("params") or {}
    raw_input = params.get("input")
    prompt = _prompt_from_input(raw_input)
    if not prompt.strip():
        return {"text": "empty input — nothing to delegate"}

    requested = payload.get("agent_name") or ""
    backend_name = requested if requested in backends else next(iter(backends))
    backend = backends[backend_name]

    try:
        session = await backend.execute(prompt, working_dir)
        result = await session.result()
    except Exception as exc:
        logger.exception("delegate handler: backend %s crashed", backend_name)
        return {"text": f"delegate backend crashed: {exc}"[:_REPLY_CAP]}

    return {"text": (result.summary or "")[:_REPLY_CAP]}


def _prompt_from_input(raw: Any) -> str:
    """Coerce `params.input` to a prompt string.

    Strings pass through. Dicts carrying a `message` key surface that;
    other dicts are stringified compactly so the brain still sees them.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        msg = raw.get("message")
        if isinstance(msg, str):
            return msg
        # Best-effort serialization — keep it small enough for a prompt.
        import json
        try:
            return json.dumps(raw, sort_keys=True)
        except Exception:
            return str(raw)
    return ""
