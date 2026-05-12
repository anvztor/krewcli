"""Slice 4 — krewcli bridge MCP server tests.

The bridge exposes a single tool `delegate(to, input, ...)` to the
in-sandbox claude/codex/gemini brain. The tool POSTs to krewhub's
`/api/v1/invocations`, long-polls until the invocation reaches a
terminal state, and returns the ResultEnvelope as the tool result.

Two test layers:
1. `delegate()` core function — mocks httpx, verifies POST + long-poll
   semantics and the ResultEnvelope shape it returns.
2. JSON-RPC stdio handler — boots the server in-process, sends
   `initialize`, `tools/list`, `tools/call`, asserts responses.

Status: RED — module doesn't exist yet.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest


# ---------------------------------------------------------------------------
# 1. delegate() — core HTTP function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_posts_invocation_and_polls_until_done(monkeypatch):
    from krewcli.mcp_servers import bridge

    posted: list[dict] = []
    polled: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int, body: dict):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json, "headers": headers})
            return FakeResponse(200, {
                "invocation_id": "inv_abc123",
                "tape_id": "tape_xyz",
                "status": "running",
            })

        async def get(self, url, headers=None, params=None):
            polled.append(url)
            # Return done on the second poll
            if len(polled) >= 2:
                return FakeResponse(200, {
                    "events": [
                        {"id": 0, "kind": "started", "payload": {}},
                        {"id": 1, "kind": "done", "payload": {
                            "result": {"action": "accept", "content": "ok", "reason": None},
                        }},
                    ],
                    "next_after": 1,
                })
            return FakeResponse(200, {"events": [
                {"id": 0, "kind": "started", "payload": {}},
            ], "next_after": 0})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")

    result = await bridge.delegate({
        "to": "sandbox:sbx_1",
        "input": "echo hi",
    })

    # POST happened with the right shape
    assert len(posted) == 1
    assert posted[0]["url"].endswith("/api/v1/invocations")
    assert posted[0]["json"]["target"] == "sandbox:sbx_1"
    assert posted[0]["json"]["input"] == "echo hi"
    assert posted[0]["headers"]["Authorization"] == "Bearer tok_test"

    # Polling happened until done
    assert len(polled) >= 2
    assert all("/events" in url for url in polled)

    # ResultEnvelope returned
    assert result == {"action": "accept", "content": "ok", "reason": None}


@pytest.mark.asyncio
async def test_delegate_returns_terminal_envelope_on_first_poll(monkeypatch):
    """If the invocation is already terminal when the first poll fires,
    delegate() returns immediately."""
    from krewcli.mcp_servers import bridge

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp({"invocation_id": "inv_1", "tape_id": "t_1", "status": "running"})

        async def get(self, *a, **k):
            return _Resp({"events": [
                {"id": 0, "kind": "started", "payload": {}},
                {"id": 1, "kind": "done", "payload": {
                    "result": {"action": "error", "content": None, "reason": "boom"},
                }},
            ], "next_after": 1})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")

    result = await bridge.delegate({"to": "sandbox:sbx_1", "input": "x"})
    assert result["action"] == "error"
    assert result["reason"] == "boom"


@pytest.mark.asyncio
async def test_delegate_post_failure_returns_error_envelope(monkeypatch):
    from krewcli.mcp_servers import bridge

    class _Resp:
        status_code = 500
        text = "internal server error"

        def json(self):
            return {"detail": "boom"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

        async def get(self, *a, **k):
            raise AssertionError("should not poll if POST failed")

    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")

    result = await bridge.delegate({"to": "sandbox:sbx_1", "input": "x"})
    assert result["action"] == "error"
    assert "500" in (result.get("reason") or "")


@pytest.mark.asyncio
async def test_delegate_generates_idempotency_key_when_omitted(monkeypatch):
    """contract §13.3: harness generates idempotency_key by default
    so a retried tool call collapses to the same Invocation."""
    from krewcli.mcp_servers import bridge

    posted: list[dict] = []

    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json, "headers": headers})
            return _Resp({"invocation_id": "inv_x", "tape_id": "t_x", "status": "running"})

        async def get(self, *a, **k):
            return _Resp({"events": [
                {"id": 0, "kind": "started", "payload": {}},
                {"id": 1, "kind": "done", "payload": {
                    "result": {"action": "accept", "content": "ok", "reason": None},
                }},
            ], "next_after": 1})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")
    monkeypatch.setenv("KREWHUB_PARENT_TAPE_ID", "parent_tape_xyz")

    await bridge.delegate({"to": "sandbox:sbx_1", "input": "x"})

    body = posted[0]["json"]
    assert body.get("idempotency_key"), "delegate must generate idempotency_key when caller omits it"
    assert body["parent_tape_id"] == "parent_tape_xyz"


@pytest.mark.asyncio
async def test_delegate_caller_idempotency_key_wins(monkeypatch):
    from krewcli.mcp_servers import bridge

    posted: list[dict] = []

    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None):
            posted.append(json)
            return _Resp({"invocation_id": "inv_x", "tape_id": "t_x", "status": "running"})

        async def get(self, *a, **k):
            return _Resp({"events": [
                {"id": 0, "kind": "started", "payload": {}},
                {"id": 1, "kind": "done", "payload": {
                    "result": {"action": "accept", "content": "ok", "reason": None},
                }},
            ], "next_after": 1})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")

    await bridge.delegate({
        "to": "sandbox:sbx_1",
        "input": "x",
        "idempotency_key": "caller_chose_this",
    })
    assert posted[0]["idempotency_key"] == "caller_chose_this"


# ---------------------------------------------------------------------------
# 2. JSON-RPC stdio handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_initialize_request():
    """JSON-RPC `initialize` returns server capabilities + tool support."""
    from krewcli.mcp_servers.bridge import handle_message

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    }
    resp = await handle_message(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "capabilities" in resp["result"]
    assert "tools" in resp["result"]["capabilities"]


@pytest.mark.asyncio
async def test_handle_tools_list_returns_delegate():
    """JSON-RPC `tools/list` returns exactly one tool: `delegate`."""
    from krewcli.mcp_servers.bridge import handle_message

    resp = await handle_message({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    })
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "delegate"
    schema = tools[0]["inputSchema"]
    assert "to" in schema["properties"]
    assert "input" in schema["properties"]
    assert "to" in schema["required"]
    assert "input" in schema["required"]


@pytest.mark.asyncio
async def test_input_schema_accepts_string_or_object():
    """Verified during the 2026-05-08 brain smoke: with no type on
    `input`, Claude Sonnet 4.6 serialized object args as JSON strings,
    making SandboxHand fall through to legacy op:exec. Declaring an
    explicit anyOf:[string, object] union forces a real object on
    structured calls."""
    from krewcli.mcp_servers.bridge import DELEGATE_TOOL_DEF

    input_schema = DELEGATE_TOOL_DEF["inputSchema"]["properties"]["input"]
    assert "anyOf" in input_schema
    types = sorted(s["type"] for s in input_schema["anyOf"])
    assert types == ["object", "string"]


@pytest.mark.asyncio
async def test_handle_tools_call_invokes_delegate(monkeypatch):
    from krewcli.mcp_servers import bridge

    async def fake_delegate(args):
        assert args == {"to": "sandbox:sbx_1", "input": "echo hi"}
        return {"action": "accept", "content": "ok", "reason": None}

    monkeypatch.setattr(bridge, "delegate", fake_delegate)

    resp = await bridge.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "delegate",
            "arguments": {"to": "sandbox:sbx_1", "input": "echo hi"},
        },
    })
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 3
    # MCP tool_result content must be a list of content blocks.
    content = resp["result"]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["action"] == "accept"


@pytest.mark.asyncio
async def test_handle_unknown_method_returns_method_not_found():
    from krewcli.mcp_servers.bridge import handle_message

    resp = await handle_message({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "made/up",
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32601  # JSON-RPC method-not-found


@pytest.mark.asyncio
async def test_handle_unknown_tool_returns_error():
    from krewcli.mcp_servers.bridge import handle_message

    resp = await handle_message({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "not_a_tool", "arguments": {}},
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32602  # invalid params


@pytest.mark.asyncio
async def test_notification_is_silent():
    """Per JSON-RPC, a notification (no `id`) gets no response."""
    from krewcli.mcp_servers.bridge import handle_message

    resp = await handle_message({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    assert resp is None


# ---------------------------------------------------------------------------
# Bare `to: "sandbox"` — krewhub-side resolution (Slice A, 2026-05-11)
# Bridge forwards bare target as-is + includes bundle_id in body so
# krewhub's invocation route can resolve via SandboxService.ensure_
# sandbox_for_bundle (provisioning if needed). Brain never sees substrate
# state; the human is never asked about sandbox lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_sandbox_target_includes_bundle_id_in_body(monkeypatch):
    """Bridge forwards bare `to: "sandbox"` unchanged + adds bundle_id
    from KREWHUB_BUNDLE_ID env to the body. Krewhub's route then
    resolves to the bundle's current sandbox (provisioning if needed)."""
    from krewcli.mcp_servers import bridge

    posted: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
        def json(self): return self._body
        @property
        def text(self): return json.dumps(self._body)

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json})
            return FakeResponse(200, {"invocation_id": "inv_x"})
        async def get(self, url, headers=None, params=None):
            return FakeResponse(200, {"events": [
                {"id": 0, "kind": "done", "payload": {
                    "result": {"action": "accept", "content": None, "reason": None},
                }}
            ], "next_after": 0})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_BUNDLE_ID", "bun_test_42")
    # KREWHUB_SANDBOX_ID is intentionally NOT set — bridge no longer
    # needs it to resolve bare target.
    monkeypatch.delenv("KREWHUB_SANDBOX_ID", raising=False)

    result = await bridge.delegate({
        "to": "sandbox",
        "input": {"op": "exec", "command": "echo hi"},
    })

    assert result["action"] == "accept"
    # Bare target forwarded as-is — krewhub does the resolving.
    assert posted[0]["json"]["target"] == "sandbox"
    assert posted[0]["json"]["bundle_id"] == "bun_test_42"


@pytest.mark.asyncio
async def test_bundle_id_carried_even_for_explicit_sandbox(monkeypatch):
    """Bundle id rides on every request (harmless when target is already
    explicit); makes the krewhub side simpler — it can always trust
    bundle_id is there."""
    from krewcli.mcp_servers import bridge

    posted: list[dict] = []

    class FakeResponse:
        def __init__(self, sc, body): self.status_code, self._body = sc, body
        def json(self): return self._body
        @property
        def text(self): return json.dumps(self._body)

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posted.append({"json": json})
            return FakeResponse(200, {"invocation_id": "inv_y"})
        async def get(self, url, headers=None, params=None):
            return FakeResponse(200, {"events": [
                {"id": 0, "kind": "done", "payload": {
                    "result": {"action": "accept", "content": None, "reason": None},
                }}
            ], "next_after": 0})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_BUNDLE_ID", "bun_e")

    await bridge.delegate({
        "to": "sandbox:sbx_explicit",
        "input": {"op": "list", "path": "/"},
    })
    assert posted[0]["json"]["target"] == "sandbox:sbx_explicit"
    assert posted[0]["json"]["bundle_id"] == "bun_e"


@pytest.mark.asyncio
async def test_explicit_sandbox_id_passes_through_unchanged(monkeypatch):
    """`to: "sandbox:<explicit_id>"` must NOT be rewritten by the auto-
    resolver — the brain may have a reason to target a specific VM
    other than its bundle's default."""
    from krewcli.mcp_servers import bridge

    posted: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
        def json(self): return self._body
        @property
        def text(self): return json.dumps(self._body)

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json})
            return FakeResponse(200, {"invocation_id": "inv_x"})
        async def get(self, url, headers=None, params=None):
            return FakeResponse(200, {"events": [
                {"id": 0, "kind": "done", "payload": {
                    "result": {"action": "accept", "content": None, "reason": None},
                }}
            ], "next_after": 0})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SANDBOX_ID", "sbx_bundle_default")

    await bridge.delegate({
        "to": "sandbox:sbx_explicit_other",
        "input": {"op": "list", "path": "/"},
    })

    # Explicit id wins; auto-resolution did not fire.
    assert posted[0]["json"]["target"] == "sandbox:sbx_explicit_other"


# ---------------------------------------------------------------------------
# Non-blocking delegate (PR2) — short poll window + action=pending
# ---------------------------------------------------------------------------


def _make_fake_client(
    invocation_id: str = "inv_test_pending",
    done_after_polls: int | None = None,
    posted_log: list | None = None,
):
    """Helper: build a FakeAsyncClient that returns one invocation and
    optionally a `done` event after N polls. If `done_after_polls` is
    None, polling never yields a done event."""
    polled: list[str] = []

    class _Resp:
        def __init__(self, status_code: int, body: dict):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if posted_log is not None:
                posted_log.append({"url": url, "json": json})
            return _Resp(200, {
                "invocation_id": invocation_id,
                "tape_id": "tape_xyz",
                "status": "running",
            })

        async def get(self, url, headers=None, params=None):
            polled.append(url)
            if done_after_polls is not None and len(polled) >= done_after_polls:
                return _Resp(200, {"events": [
                    {"id": 0, "kind": "done", "payload": {
                        "result": {"action": "accept", "content": "answered",
                                   "reason": None},
                    }},
                ], "next_after": 0})
            return _Resp(200, {"events": [], "next_after": -1})

    return _FakeClient, polled


@pytest.mark.asyncio
async def test_nonblocking_returns_pending_after_window(monkeypatch):
    """With KREWHUB_DELEGATE_NONBLOCKING=1, if the operator hasn't
    answered before the short poll window elapses, delegate returns
    action=pending with the invocation_id — letting the brain end its
    turn cleanly. PR2 contract."""
    from krewcli.mcp_servers import bridge

    FakeClient, polled = _make_fake_client(done_after_polls=None)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")
    monkeypatch.setenv("KREWHUB_DELEGATE_NONBLOCKING", "1")
    monkeypatch.setenv("KREWHUB_DELEGATE_POLL_WINDOW_S", "0.3")  # ~300ms

    result = await bridge.delegate({"to": "human", "input": "pick A or B"})

    assert result["action"] == "pending", result
    assert result["reason"] == "awaiting_operator"
    assert result["content"]["invocation_id"] == "inv_test_pending"
    # Bridge polled at least once before bailing.
    assert len(polled) >= 1


@pytest.mark.asyncio
async def test_nonblocking_fast_path_still_returns_terminal(monkeypatch):
    """Non-blocking mode preserves the fast path: if the operator
    answers within the poll window, delegate returns the terminal
    envelope just like the legacy blocking path. No spurious
    `pending`."""
    from krewcli.mcp_servers import bridge

    FakeClient, polled = _make_fake_client(done_after_polls=2)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")
    monkeypatch.setenv("KREWHUB_DELEGATE_NONBLOCKING", "1")
    monkeypatch.setenv("KREWHUB_DELEGATE_POLL_WINDOW_S", "10")  # plenty

    result = await bridge.delegate({"to": "human", "input": "any answer?"})

    assert result["action"] == "accept"
    assert result["content"] == "answered"


@pytest.mark.asyncio
async def test_blocking_mode_unchanged_by_default(monkeypatch):
    """Without KREWHUB_DELEGATE_NONBLOCKING, the bridge behaves as
    before: blocks until the invocation deadline expires, then returns
    an error envelope (not pending). Backwards-compat invariant for
    PR2 — operators not flipped over keep today's UX."""
    from krewcli.mcp_servers import bridge

    FakeClient, polled = _make_fake_client(done_after_polls=None)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")
    monkeypatch.delenv("KREWHUB_DELEGATE_NONBLOCKING", raising=False)

    result = await bridge.delegate({
        "to": "human", "input": "x", "deadline_s": 1,
    })

    assert result["action"] == "error"
    assert "timeout" in (result.get("reason") or "")


@pytest.mark.asyncio
async def test_task_id_from_env_lands_in_invocation_body(monkeypatch):
    """When KREWHUB_TASK_ID is set in the bridge's env (the daemon's
    exec env always exports it), delegate must include it in the POST
    body so PR1's projection knows which task tape to append to."""
    from krewcli.mcp_servers import bridge

    posted: list = []
    FakeClient, _ = _make_fake_client(done_after_polls=1, posted_log=posted)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")
    monkeypatch.setenv("KREWHUB_TASK_ID", "task_e714588a")

    await bridge.delegate({"to": "human", "input": "test"})

    assert posted, "expected one POST to /invocations"
    assert posted[0]["json"].get("task_id") == "task_e714588a", posted[0]["json"]


@pytest.mark.asyncio
async def test_task_id_absent_when_env_unset(monkeypatch):
    """Bridges spawned outside a task context (no KREWHUB_TASK_ID env)
    must NOT include `task_id` in the body — PR1 falls back to
    non-projection cleanly for those."""
    from krewcli.mcp_servers import bridge

    posted: list = []
    FakeClient, _ = _make_fake_client(done_after_polls=1, posted_log=posted)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setenv("KREWHUB_URL", "http://krewhub:8420")
    monkeypatch.setenv("KREWHUB_SESSION_TOKEN", "tok_test")
    monkeypatch.delenv("KREWHUB_TASK_ID", raising=False)

    await bridge.delegate({"to": "human", "input": "test"})

    assert posted, "expected one POST to /invocations"
    assert "task_id" not in posted[0]["json"], posted[0]["json"]
