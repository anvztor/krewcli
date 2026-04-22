"""Unit tests for the backend protocol and implementations."""

from __future__ import annotations

import asyncio

import pytest

from krewcli.backend.protocol import BackendMessage, BackendResult, BackendSession
from krewcli.backend.echo import EchoBackend
from krewcli.backend.registry import get_backend, available_backends


class TestBackendProtocol:
    def test_backend_message_is_frozen(self):
        msg = BackendMessage(kind="agent_reply", body="hello", payload={"text": "hello"})
        assert msg.kind == "agent_reply"
        assert msg.body == "hello"

    def test_backend_result_is_frozen(self):
        result = BackendResult(success=True, summary="done")
        assert result.success is True
        assert result.files_modified == []
        assert result.blocked_reason is None

    @pytest.mark.asyncio
    async def test_backend_session_messages_iter(self):
        queue: asyncio.Queue[BackendMessage | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[BackendResult] = loop.create_future()

        session = BackendSession(messages=queue, result_future=future)

        await queue.put(BackendMessage(kind="agent_reply", body="hi"))
        await queue.put(BackendMessage(kind="session_end", body="done"))
        await queue.put(None)  # sentinel

        messages = []
        async for msg in session.messages_iter():
            messages.append(msg)

        assert len(messages) == 2
        assert messages[0].kind == "agent_reply"
        assert messages[1].kind == "session_end"


class TestEchoBackend:
    @pytest.mark.asyncio
    async def test_echo_produces_expected_events(self):
        backend = EchoBackend()
        assert backend.name == "echo"
        assert await backend.health() is True

        session = await backend.execute("test prompt", "/tmp")

        kinds = []
        async for msg in session.messages_iter():
            kinds.append(msg.kind)

        assert "session_start" in kinds
        assert "agent_reply" in kinds
        assert "session_end" in kinds
        assert "tool_use" in kinds
        assert "tool_result" in kinds

        result = await session.result()
        assert result.success is True
        assert "test prompt" in result.summary


class TestRegistry:
    def test_available_backends_includes_echo(self):
        backends = available_backends()
        assert "echo" in backends
        assert "claude" in backends
        assert "codex" in backends
        assert "bub" in backends

    def test_get_backend_echo(self):
        backend = get_backend("echo")
        assert backend.name == "echo"

    def test_get_backend_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend("nonexistent")
