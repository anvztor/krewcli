"""Daemon-side handler for `method="delegate"` (Invocation Contract §10.3).

When krewhub's AgentHand creates an A2A row with method="delegate",
the SSEWatcher pulls it and calls _handle_invocation. This slice adds
a method dispatcher: if method=="delegate", we run the named backend
against `params.input` and return the reply as `{text: <reply>}`.

No task lifecycle, no bundle, no krewhub round-trip — just spawn the
brain, capture the result, return.

Tests use a fake Backend that returns a scripted BackendResult. No
subprocess, no krewhub. Status: RED.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    name_: str
    summary: str = "fake reply"
    success: bool = True
    full_output: str = ""

    @property
    def name(self) -> str:
        return self.name_

    async def execute(self, prompt, working_dir, *, env=None):
        from krewcli.backend.protocol import BackendResult, BackendSession
        msgs: asyncio.Queue = asyncio.Queue()
        await msgs.put(None)  # immediate completion
        result_future: asyncio.Future = asyncio.get_event_loop().create_future()
        # Stash the prompt so tests can assert on it.
        self.last_prompt = prompt
        self.last_working_dir = working_dir
        result_future.set_result(BackendResult(
            success=self.success,
            summary=self.summary,
            full_output=self.full_output,
        ))
        return BackendSession(messages=msgs, result_future=result_future)

    async def health(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Pure handler unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_delegate_returns_brain_reply():
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    backends = {"claude": _FakeBackend(name_="claude", summary="2+2 = 4")}
    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": "what is 2+2?"},
    }
    result = await handle_delegate_invocation(payload, backends, working_dir="/tmp")
    assert result == {"text": "2+2 = 4"}
    assert backends["claude"].last_prompt == "what is 2+2?"


@pytest.mark.asyncio
async def test_handle_delegate_dict_input_serializes_message_field():
    """Dict input — extract `message` if present, else stringify the dict."""
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    backends = {"claude": _FakeBackend(name_="claude", summary="ok")}
    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": {"message": "extracted msg", "tone": "casual"}},
    }
    await handle_delegate_invocation(payload, backends, working_dir="/tmp")
    assert backends["claude"].last_prompt == "extracted msg"


@pytest.mark.asyncio
async def test_handle_delegate_unknown_agent_falls_back_to_first_backend():
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    claude = _FakeBackend(name_="claude", summary="from claude")
    backends = {"claude": claude}
    payload = {
        "method": "delegate",
        "agent_name": "unknown",
        "params": {"input": "hi"},
    }
    result = await handle_delegate_invocation(payload, backends, working_dir="/tmp")
    assert result == {"text": "from claude"}


@pytest.mark.asyncio
async def test_handle_delegate_no_backends_returns_protest():
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": "hi"},
    }
    result = await handle_delegate_invocation(payload, {}, working_dir="/tmp")
    assert "no" in result["text"].lower() and "backend" in result["text"].lower()


@pytest.mark.asyncio
async def test_handle_delegate_empty_input_returns_protest():
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    backends = {"claude": _FakeBackend(name_="claude")}
    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": ""},
    }
    result = await handle_delegate_invocation(payload, backends, working_dir="/tmp")
    assert "empty" in result["text"].lower() or "input" in result["text"].lower()


@pytest.mark.asyncio
async def test_handle_delegate_backend_failure_surfaces_summary():
    """Even on backend.success=False, return the summary text — failures
    are values, the caller (krewhub AgentHand) decides how to translate."""
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    backends = {"claude": _FakeBackend(
        name_="claude", success=False, summary="model timed out",
    )}
    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": "hi"},
    }
    result = await handle_delegate_invocation(payload, backends, working_dir="/tmp")
    assert "model timed out" in result["text"]


@pytest.mark.asyncio
async def test_handle_delegate_truncates_long_replies():
    from krewcli.daemon.delegate_handler import handle_delegate_invocation

    big = "X" * 8192
    backends = {"claude": _FakeBackend(name_="claude", summary=big)}
    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": "hi"},
    }
    result = await handle_delegate_invocation(payload, backends, working_dir="/tmp")
    # Cap at 4096 to match _handle_invocation's existing summary cap
    assert len(result["text"]) <= 4096


# ---------------------------------------------------------------------------
# DaemonLoop._handle_invocation method routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_handle_invocation_routes_method_delegate():
    """When SSEWatcher delivers a payload with method='delegate',
    DaemonLoop._handle_invocation routes to the delegate handler
    instead of the legacy task-shaped path that protests
    'no task_id or bundle_id in metadata'."""
    from krewcli.daemon.loop import DaemonLoop

    # Construct a minimal DaemonLoop without going through start(), which
    # spawns subprocesses + heartbeats. We only exercise the method
    # dispatch logic.
    loop = DaemonLoop.__new__(DaemonLoop)
    loop._backends = {"claude": _FakeBackend(name_="claude", summary="42")}
    loop._working_dir = "/tmp"
    loop._owner = "krew"
    loop._agent_ids = {"claude": "claude@krew"}
    loop._running_tasks = set()
    loop._planning_bundle = None
    loop._client = None  # type: ignore[assignment]

    payload = {
        "method": "delegate",
        "agent_name": "claude",
        "params": {"input": "hello"},
        "id": "a2a_test",
    }
    result = await loop._handle_invocation(payload)
    assert result == {"text": "42"}
