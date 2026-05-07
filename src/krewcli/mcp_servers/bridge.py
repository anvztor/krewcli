"""krewcli-bridge — stdio MCP server exposing the `delegate` tool.

Runs inside the e2b sandbox alongside `claude -p`. Bridges the
single tool surface a brain needs (`delegate(target, input, ...)`) to
krewhub's `/api/v1/invocations` HTTP API.

Aligned with Anthropic Managed Agents' `execute(name, input) → string`
primitive. The model sees one verb; the bridge handles the round-trip.

Wire format: minimal JSON-RPC 2.0 over stdio. The `mcp` Python SDK is
not vendored to keep sandbox runtime small.

Run: `python -m krewcli.mcp_servers.bridge`

Required env vars:
- KREWHUB_URL              base URL (e.g. http://krewhub:8420)
- KREWHUB_SESSION_TOKEN    bearer for `/api/v1/invocations`
- KREWHUB_TASK_ID          surfaced for telemetry
- KREWHUB_PARENT_TAPE_ID   default parent for delegate's child tape

Optional:
- KREWHUB_POLL_TIMEOUT_S   per-poll timeout (default 30s)
- KREWHUB_DELEGATE_DEFAULT_DEADLINE_S  default tool deadline (300s)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from typing import Any

import httpx


logger = logging.getLogger("krewcli.bridge")


# ---------------------------------------------------------------------------
# Tool definition (MCP `tools/list` shape)
# ---------------------------------------------------------------------------


DELEGATE_TOOL_DEF: dict = {
    "name": "delegate",
    "description": (
        "Invoke a Cookrew Hand (sandbox, human, or sub-agent) and wait for "
        "its ResultEnvelope. Use this for any work outside your own reasoning "
        "context: run an e2b sandbox command, ask the human operator, or "
        "dispatch a peer agent. Do NOT use AskUserQuestion — there is no "
        "local UI."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["to", "input"],
        "properties": {
            "to": {
                "type": "string",
                "description": (
                    "Target. One of: 'sandbox:<sandbox_id>', 'human', "
                    "'agent:<agent_id>'."
                ),
            },
            "input": {
                "description": (
                    "What to send the Hand. String for humans/sandboxes, "
                    "structured for agents."
                ),
            },
            "schema": {
                "type": "object",
                "description": (
                    "MCP-elicitation-subset JSON Schema. When set, the Hand "
                    "validates ResultEnvelope.content against it."
                ),
            },
            "deadline_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": 86400,
                "description": "Hard timeout. Default 300.",
            },
            "label": {
                "type": "string",
                "maxLength": 60,
                "description": (
                    "Optional short tag for the operator's mission board. "
                    "Defaults to first 60 chars of input."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "De-dupes retries of the same delegate call. If omitted, "
                    "the bridge generates one from "
                    "(parent_tape, target, input)."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# delegate() — POST + long-poll loop
# ---------------------------------------------------------------------------


def _krewhub_url() -> str:
    return os.environ.get("KREWHUB_URL", "").rstrip("/")


def _session_token() -> str:
    return os.environ.get("KREWHUB_SESSION_TOKEN", "")


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    tok = _session_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _generate_idempotency_key(parent_tape_id: str, args: dict) -> str:
    """Stable hash of (parent, target, input) so retries collapse."""
    payload = json.dumps(
        {
            "parent": parent_tape_id,
            "to": args.get("to"),
            "input": args.get("input"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


async def delegate(args: dict) -> dict:
    """Send `args` as a POST body to /api/v1/invocations, then long-poll
    /events until a `done` event lands. Return the ResultEnvelope (the
    `done` payload's `result` field), or an `error` envelope on failure."""
    base = _krewhub_url()
    if not base:
        return {
            "action": "error", "content": None,
            "reason": "bridge_misconfig: KREWHUB_URL unset",
        }

    parent_tape_id = (
        args.get("parent_tape_id")
        or os.environ.get("KREWHUB_PARENT_TAPE_ID")
        or ""
    )
    body: dict = {
        "target": args["to"],
        "input": args["input"],
    }
    if "schema" in args:
        body["schema"] = args["schema"]
    if "deadline_s" in args:
        body["deadline_s"] = args["deadline_s"]
    else:
        try:
            body["deadline_s"] = int(
                os.environ.get("KREWHUB_DELEGATE_DEFAULT_DEADLINE_S", "300"),
            )
        except ValueError:
            body["deadline_s"] = 300
    if "label" in args:
        body["label"] = args["label"]
    if parent_tape_id:
        body["parent_tape_id"] = parent_tape_id
    # Tag with recipe_id (if set) so invocation events fan out through
    # the cookrew operator's recipe SSE stream — they see HITL elicits
    # appear in real time without polling.
    recipe_id = os.environ.get("KREWHUB_RECIPE_ID")
    if recipe_id:
        body["recipe_id"] = recipe_id
    body["idempotency_key"] = (
        args.get("idempotency_key")
        or _generate_idempotency_key(parent_tape_id, args)
    )

    poll_timeout = float(os.environ.get("KREWHUB_POLL_TIMEOUT_S", "30"))
    overall_deadline = time.monotonic() + body["deadline_s"] + 30.0

    try:
        async with httpx.AsyncClient(timeout=poll_timeout) as client:
            create = await client.post(
                f"{base}/api/v1/invocations",
                json=body,
                headers=_headers(),
            )
            if create.status_code != 200:
                return {
                    "action": "error", "content": None,
                    "reason": (
                        f"invocation_create_failed: {create.status_code} "
                        f"{create.text[:200]}"
                    ),
                }
            payload = create.json()
            invocation_id = payload.get("invocation_id")
            if not invocation_id:
                return {
                    "action": "error", "content": None,
                    "reason": f"invocation_create_bad_body: {payload!r}",
                }

            after = -1
            while True:
                if time.monotonic() > overall_deadline:
                    return {
                        "action": "error", "content": None,
                        "reason": "delegate_timeout: overall deadline exceeded",
                    }
                params: dict = {}
                if after >= 0:
                    params["after"] = after
                events_resp = await client.get(
                    f"{base}/api/v1/invocations/{invocation_id}/events",
                    headers=_headers(),
                    params=params or None,
                )
                if events_resp.status_code != 200:
                    return {
                        "action": "error", "content": None,
                        "reason": (
                            f"events_poll_failed: {events_resp.status_code} "
                            f"{events_resp.text[:200]}"
                        ),
                    }
                eb = events_resp.json()
                events = eb.get("events", [])
                for ev in events:
                    if ev.get("kind") == "done":
                        env = (ev.get("payload") or {}).get("result")
                        if isinstance(env, dict):
                            return env
                        return {
                            "action": "error", "content": None,
                            "reason": "done_without_result",
                        }
                    if isinstance(ev.get("id"), int) and ev["id"] > after:
                        after = ev["id"]
                # short backoff before next poll
                await asyncio.sleep(0.25)
    except Exception as exc:
        return {
            "action": "error", "content": None,
            "reason": f"bridge_exception: {exc}",
        }


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 stdio handler
# ---------------------------------------------------------------------------


_PROTOCOL_VERSION = "2025-06-18"


async def handle_message(msg: dict) -> dict | None:
    """Dispatch one JSON-RPC message. Returns the response dict or None
    for notifications (which by spec get no response)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method is None:
        if is_notification:
            return None
        return _error_response(msg_id, -32600, "invalid_request: missing method")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "krewcli-bridge", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [DELEGATE_TOOL_DEF]}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            if name != "delegate":
                if is_notification:
                    return None
                return _error_response(
                    msg_id, -32602,
                    f"unknown_tool: {name!r} (only 'delegate' is exposed)",
                )
            args = params.get("arguments") or {}
            envelope = await delegate(args)
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(envelope)},
                ],
                "isError": envelope.get("action") == "error",
            }
        elif method.startswith("notifications/"):
            # Client lifecycle notification — silent ack.
            return None
        else:
            if is_notification:
                return None
            return _error_response(msg_id, -32601, f"method_not_found: {method}")
    except Exception as exc:
        if is_notification:
            return None
        return _error_response(msg_id, -32603, f"internal_error: {exc}")

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------------------
# stdio loop
# ---------------------------------------------------------------------------


async def serve_stdio() -> None:
    """Read JSON-RPC messages from stdin, write responses to stdout.

    Each message is on its own line (the simplest framing). MCP's
    full spec also supports Content-Length framing; we keep
    line-delimited for headless robustness.
    """
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            sys.stderr.write(f"krewcli-bridge: bad JSON line: {text!r}\n")
            continue
        resp = await handle_message(msg)
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("KREWCLI_BRIDGE_LOG_LEVEL", "WARNING"),
        format="%(asctime)s [%(levelname)s] krewcli-bridge: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(serve_stdio())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
