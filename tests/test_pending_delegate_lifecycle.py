"""PR3 — daemon-side wiring for non-blocking delegate.

When the bridge returns `action: pending` (because the operator hasn't
answered within the short poll window), the harness must:

  1. Detect the pending invocation_id from the brain's tool_result
     stream.
  2. Set task status to `blocked` (with blocked_reason referencing the
     invocation) at session end, instead of `done` — cookrew-beta's
     frontend derives `hitl='needs_input'` from `status='blocked'` and
     auto-opens the HITL popout.

Also covers:
  - `_bridge_env` forwards the PR2 flag (KREWHUB_DELEGATE_NONBLOCKING +
    poll window) from the daemon's parent env into the bridge.
  - `_pending_invocation_id` correctly parses pending envelopes and
    ignores everything else (sandbox calls, non-pending delegates,
    malformed JSON).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from krewcli.backend.protocol import BackendMessage, BackendResult
from krewcli.daemon.harness import Harness, _pending_invocation_id
from krewcli.daemon.session import Session

# Re-use the test fixtures from the existing integration suite.
from tests.test_sandbox_harness_integration import (
    _FakeBackend,
    _FakeClient,
    _FakeExecEnv,
)


# ── Pure helper tests ───────────────────────────────────────────────


def test_pending_invocation_id_parses_pending_envelope():
    payload = {
        "tool_use_id": "tu_1",
        "output": json.dumps({
            "action": "pending",
            "content": {"invocation_id": "inv_abc123"},
            "reason": "awaiting_operator",
        }),
    }
    assert _pending_invocation_id(payload) == "inv_abc123"


def test_pending_invocation_id_ignores_terminal_envelopes():
    """Accept / decline / cancel / error all return None — only the new
    `pending` action triggers the PR3 lifecycle transition."""
    for action in ("accept", "decline", "cancel", "error"):
        body = {
            "action": action,
            "content": "whatever" if action == "accept" else None,
            "reason": "x" if action != "accept" else None,
        }
        payload = {"output": json.dumps(body)}
        assert _pending_invocation_id(payload) is None, action


def test_pending_invocation_id_ignores_non_delegate_tool_results():
    """Sandbox exec results, MCP tools we don't care about, etc. —
    these are valid JSON dicts but lack an `action` field. Must not
    fire the pending detector."""
    payload = {"output": json.dumps({"stdout": "hello world", "rc": 0})}
    assert _pending_invocation_id(payload) is None


def test_pending_invocation_id_handles_malformed_output():
    """Whatever the backend wraps in `output` may not be JSON at all —
    a plaintext tool reply, an error string, an empty body. None of
    these should crash or false-positive."""
    for bad in ("not json", "", None, 42, "<html>x</html>"):
        assert _pending_invocation_id({"output": bad}) is None


def test_pending_invocation_id_pending_without_invocation_id():
    """Defensive fallback: a pending envelope missing the
    invocation_id still flags `pending` so the harness blocks the
    task — just with a sentinel id so the operator can recover."""
    payload = {"output": json.dumps({"action": "pending"})}
    assert _pending_invocation_id(payload) == "unknown_invocation"


# ── Harness lifecycle tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_harness_blocks_task_when_brain_returns_pending(tmp_path: Path):
    """Brain streams a tool_result with action=pending. The harness
    must call update_task_status with `blocked`, not `done`, and
    include the invocation_id in blocked_reason so the operator
    knows what's waiting."""
    workdir = tmp_path / "wd"
    workdir.mkdir()

    client = _FakeClient()
    harness = Harness(client)

    # A successful run that included one delegate(human) call which
    # came back pending. The brain emitted an agent_reply summarizing
    # what it asked, then ended its turn.
    pending_envelope = {
        "action": "pending",
        "content": {"invocation_id": "inv_pending_xyz"},
        "reason": "awaiting_operator",
    }
    messages = [
        BackendMessage(kind="session_start", body="▶ start"),
        BackendMessage(
            kind="tool_use",
            body="delegate(human, ...)",
            payload={"tool_use_id": "tu_1", "name": "delegate"},
        ),
        BackendMessage(
            kind="tool_result",
            body="→ ok",
            payload={
                "tool_use_id": "tu_1",
                "output": json.dumps(pending_envelope),
            },
        ),
        BackendMessage(
            kind="agent_reply",
            body="<p>Asked the operator for X.</p>",
            payload={"text": "<p>Asked the operator for X.</p>"},
        ),
        BackendMessage(kind="session_end", body="■ done"),
    ]
    backend = _FakeBackend(
        messages=messages,
        result=BackendResult(success=True, summary="Asked operator"),
    )
    execenv = _FakeExecEnv(workdir=str(workdir))
    session = Session(client, "task_pending", "claude@krew", flush_interval=0.05)

    result = await harness.execute(
        backend=backend, session=session, execenv=execenv,
        prompt="do thing", task_id="task_pending",
    )

    # Brain run itself succeeded — we're not failing the brain, we're
    # parking the task while the operator answers.
    assert result.success is True

    # Status was set to BLOCKED, not done.
    statuses = [s for (_, s) in client.status_updates]
    assert "done" not in statuses, statuses
    assert "blocked" in statuses, statuses
    # Last call should be the blocked transition.
    assert client.status_updates[-1] == ("task_pending", "blocked")


@pytest.mark.asyncio
async def test_harness_keeps_done_when_no_pending_in_stream(tmp_path: Path):
    """Regression: brain runs that DON'T contain a pending tool_result
    must still transition to `done` on success. PR3 only intercepts
    pending — everything else stays on the legacy path."""
    workdir = tmp_path / "wd"
    workdir.mkdir()

    client = _FakeClient()
    harness = Harness(client)

    messages = [
        BackendMessage(kind="session_start", body="▶ start"),
        BackendMessage(
            kind="tool_result",
            body="→ ok",
            payload={"tool_use_id": "tu_1",
                     "output": json.dumps({"action": "accept",
                                           "content": "done"})},
        ),
        BackendMessage(kind="session_end", body="■ done"),
    ]
    backend = _FakeBackend(
        messages=messages,
        result=BackendResult(success=True, summary="OK"),
    )
    execenv = _FakeExecEnv(workdir=str(workdir))
    session = Session(client, "task_ok", "claude@krew", flush_interval=0.05)

    await harness.execute(
        backend=backend, session=session, execenv=execenv,
        prompt="x", task_id="task_ok",
    )

    statuses = [s for (_, s) in client.status_updates]
    assert "done" in statuses, statuses
    assert "blocked" not in statuses, statuses


@pytest.mark.asyncio
async def test_harness_pending_wins_when_brain_run_succeeds(tmp_path: Path):
    """Mixed stream: one accept + one pending. PR3 errs on the side of
    surfacing the pending — the operator needs to answer something."""
    workdir = tmp_path / "wd"
    workdir.mkdir()

    client = _FakeClient()
    harness = Harness(client)

    messages = [
        BackendMessage(kind="session_start", body="▶ start"),
        BackendMessage(
            kind="tool_result",
            body="→ ok",
            payload={"tool_use_id": "tu_1",
                     "output": json.dumps({"action": "accept",
                                           "content": "first answer"})},
        ),
        BackendMessage(
            kind="tool_result",
            body="→ ok",
            payload={"tool_use_id": "tu_2",
                     "output": json.dumps({
                         "action": "pending",
                         "content": {"invocation_id": "inv_second"},
                         "reason": "awaiting_operator",
                     })},
        ),
        BackendMessage(kind="session_end", body="■ done"),
    ]
    backend = _FakeBackend(
        messages=messages,
        result=BackendResult(success=True, summary="ok"),
    )
    execenv = _FakeExecEnv(workdir=str(workdir))
    session = Session(client, "task_mixed", "claude@krew", flush_interval=0.05)

    await harness.execute(
        backend=backend, session=session, execenv=execenv,
        prompt="x", task_id="task_mixed",
    )

    assert client.status_updates[-1] == ("task_mixed", "blocked")


# ── _bridge_env propagation ─────────────────────────────────────────


def test_bridge_env_forwards_nonblocking_flag(monkeypatch):
    """When the daemon's parent env has KREWHUB_DELEGATE_NONBLOCKING=1,
    that propagates into the bridge spawn env so the bridge logic
    (PR2) sees the flag inside the sandbox."""
    from krewcli.backend._delegate import _bridge_env

    monkeypatch.setenv("KREWHUB_DELEGATE_NONBLOCKING", "1")
    monkeypatch.setenv("KREWHUB_DELEGATE_POLL_WINDOW_S", "20")

    env = _bridge_env(
        krewhub_url="http://hub", task_id="t1", session_token="tok",
        parent_tape_id="", bundle_id="b1", recipe_id="r1",
    )
    assert env["KREWHUB_DELEGATE_NONBLOCKING"] == "1"
    assert env["KREWHUB_DELEGATE_POLL_WINDOW_S"] == "20"


def test_bridge_env_omits_flag_when_not_set(monkeypatch):
    """When the daemon's parent env is clean, the flag is NOT
    injected — preserving the legacy blocking behavior for operators
    who haven't opted in."""
    from krewcli.backend._delegate import _bridge_env

    monkeypatch.delenv("KREWHUB_DELEGATE_NONBLOCKING", raising=False)
    monkeypatch.delenv("KREWHUB_DELEGATE_POLL_WINDOW_S", raising=False)

    env = _bridge_env(
        krewhub_url="http://hub", task_id="t1", session_token="tok",
        parent_tape_id="", bundle_id="b1", recipe_id="r1",
    )
    assert "KREWHUB_DELEGATE_NONBLOCKING" not in env
    assert "KREWHUB_DELEGATE_POLL_WINDOW_S" not in env
