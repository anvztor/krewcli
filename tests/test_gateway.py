"""Tests for SpawnManager, GatewayExecutor, and gateway server creation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from a2a.types import TaskState

from krewcli.a2a.executors.gateway import (
    GatewayExecutor,
    build_gateway_agent_card,
)
from krewcli.a2a.gateway_server import create_gateway_app
from krewcli.a2a.spawn_manager import SpawnManager
from krewcli.agents.base import AgentRunResult
from krewcli.agents.models import TaskResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_agent(
    success: bool = True,
    summary: str = "Done",
    blocked_reason: str | None = None,
) -> Mock:
    """Return a mock agent whose .run() resolves to a TaskResult."""
    task_result = TaskResult(
        success=success,
        summary=summary,
        blocked_reason=blocked_reason,
        files_modified=[],
        code_refs=[],
    )
    run_result = AgentRunResult(output=task_result)
    agent = Mock()
    agent.run = AsyncMock(return_value=run_result)
    return agent


def _make_request_context(
    text: str = "implement feature",
    task_id: str = "t-1",
    context_id: str = "ctx-1",
    metadata: dict | None = None,
) -> Mock:
    """Build a minimal mock RequestContext for GatewayExecutor."""
    part = Mock()
    part.root = Mock(text=text)

    message = Mock()
    message.parts = [part] if text else []
    message.metadata = metadata or {}

    # Provide a stub current_task so new_task() is never called on the mock
    current_task = Mock()

    ctx = Mock()
    ctx.message = message
    ctx.task_id = task_id
    ctx.context_id = context_id
    ctx.current_task = current_task
    return ctx


def _make_event_queue() -> Mock:
    """Build a mock EventQueue with an async enqueue_event."""
    eq = Mock()
    eq.enqueue_event = AsyncMock()
    return eq


# ---------------------------------------------------------------------------
# SpawnManager tests
# ---------------------------------------------------------------------------

class TestSpawnManagerTracksSessions:
    """Spawn with mocked agent, verify counts increment then decrement."""

    @pytest.mark.asyncio
    async def test_spawn_increments_and_decrements(self):
        mock_agent = _make_mock_agent()

        with patch("krewcli.a2a.spawn_manager.get_agent", return_value=mock_agent):
            mgr = SpawnManager(working_dir="/tmp/test")
            assert mgr.running_count == 0

            started = await mgr.spawn("claude", "a-1", "t-1", "do stuff")
            assert started is True
            assert mgr.running_count == 1

            # Let the background task finish
            await asyncio.sleep(0.05)
            assert mgr.running_count == 0


class TestSpawnManagerPreventsDuplicateTask:
    """Spawning with the same task_id twice returns False."""

    @pytest.mark.asyncio
    async def test_duplicate_task_id_rejected(self):
        mock_agent = _make_mock_agent()
        # Make .run() block so the first task stays active
        mock_agent.run = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(10))

        with patch("krewcli.a2a.spawn_manager.get_agent", return_value=mock_agent):
            mgr = SpawnManager(working_dir="/tmp/test")

            first = await mgr.spawn("claude", "a-1", "t-1", "prompt")
            assert first is True

            second = await mgr.spawn("claude", "a-1", "t-1", "prompt again")
            assert second is False

            await mgr.shutdown()


class TestSpawnManagerCancel:
    """Cancelling a task removes the session."""

    @pytest.mark.asyncio
    async def test_cancel_removes_session(self):
        mock_agent = _make_mock_agent()
        mock_agent.run = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(10))

        with patch("krewcli.a2a.spawn_manager.get_agent", return_value=mock_agent):
            mgr = SpawnManager(working_dir="/tmp/test")
            await mgr.spawn("claude", "a-1", "t-1", "prompt")
            assert mgr.running_count == 1

            cancelled = await mgr.cancel("t-1")
            assert cancelled is True
            assert mgr.running_count == 0

    @pytest.mark.asyncio
    async def test_cancel_unknown_task_returns_false(self):
        mgr = SpawnManager(working_dir="/tmp/test")
        assert await mgr.cancel("nonexistent") is False


class TestSpawnManagerRunningCountFor:
    """Per-agent-type running counts are tracked correctly."""

    @pytest.mark.asyncio
    async def test_counts_per_agent_type(self):
        mock_agent = _make_mock_agent()
        mock_agent.run = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(10))

        with patch("krewcli.a2a.spawn_manager.get_agent", return_value=mock_agent):
            mgr = SpawnManager(working_dir="/tmp/test")

            await mgr.spawn("claude", "a-1", "t-1", "prompt")
            await mgr.spawn("claude", "a-2", "t-2", "prompt")
            await mgr.spawn("codex", "a-3", "t-3", "prompt")

            assert mgr.running_count_for("claude") == 2
            assert mgr.running_count_for("codex") == 1
            assert mgr.running_count_for("bub") == 0
            assert mgr.running_count == 3

            await mgr.shutdown()


class TestSpawnManagerCallbackOnCompletion:
    """Verify callback POST is made with correct payload after task completes."""

    @pytest.mark.asyncio
    async def test_callback_posts_result(self):
        mock_agent = _make_mock_agent(success=True, summary="All done")
        mock_response = Mock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("krewcli.a2a.spawn_manager.get_agent", return_value=mock_agent),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            mgr = SpawnManager(
                working_dir="/tmp/test",
                callback_url="http://localhost:8000/callback",
                api_key="test-key",
            )

            await mgr.spawn("claude", "a-1", "t-1", "do stuff")
            await asyncio.sleep(0.05)

            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert payload["task_id"] == "t-1"
            assert payload["agent_id"] == "a-1"
            assert payload["success"] is True
            assert payload["summary"] == "All done"

            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
            assert headers["X-API-Key"] == "test-key"


# ---------------------------------------------------------------------------
# GatewayExecutor tests
# ---------------------------------------------------------------------------

class TestGatewayExecutorSpawnsOnExecute:
    """Execute calls spawn on the manager."""

    @pytest.mark.asyncio
    async def test_execute_calls_spawn(self):
        mock_spawn = Mock(spec=SpawnManager)
        mock_spawn.running_count_for = Mock(return_value=0)
        mock_spawn.spawn = AsyncMock(return_value=True)

        executor = GatewayExecutor(
            agent_name="claude",
            spawn_manager=mock_spawn,
            agent_id="a-1",
            max_concurrent=2,
        )

        ctx = _make_request_context(text="build feature", task_id="t-1")
        eq = _make_event_queue()

        await executor.execute(ctx, eq)

        mock_spawn.spawn.assert_called_once()
        call_kwargs = mock_spawn.spawn.call_args.kwargs
        assert call_kwargs["agent_name"] == "claude"
        assert call_kwargs["prompt"] == "build feature"

        # Should have enqueued: task event, then working status
        assert eq.enqueue_event.call_count == 2
        last_event = eq.enqueue_event.call_args_list[-1][0][0]
        assert last_event.status.state == TaskState.working


class TestGatewayExecutorRejectsAtCapacity:
    """At max capacity the executor enqueues a rejected event."""

    @pytest.mark.asyncio
    async def test_rejected_at_capacity(self):
        mock_spawn = Mock(spec=SpawnManager)
        mock_spawn.running_count_for = Mock(return_value=1)

        executor = GatewayExecutor(
            agent_name="claude",
            spawn_manager=mock_spawn,
            agent_id="a-1",
            max_concurrent=1,
        )

        ctx = _make_request_context()
        eq = _make_event_queue()

        await executor.execute(ctx, eq)

        # Should have enqueued: task event, then rejected status
        assert eq.enqueue_event.call_count == 2
        last_event = eq.enqueue_event.call_args_list[-1][0][0]
        assert last_event.status.state == TaskState.rejected
        assert last_event.final is True


class TestGatewayExecutorFailsWithoutPrompt:
    """Empty message produces a failed event."""

    @pytest.mark.asyncio
    async def test_no_prompt_fails(self):
        mock_spawn = Mock(spec=SpawnManager)
        mock_spawn.running_count_for = Mock(return_value=0)

        executor = GatewayExecutor(
            agent_name="claude",
            spawn_manager=mock_spawn,
            agent_id="a-1",
        )

        ctx = _make_request_context(text="")
        eq = _make_event_queue()

        await executor.execute(ctx, eq)

        assert eq.enqueue_event.call_count == 2
        last_event = eq.enqueue_event.call_args_list[-1][0][0]
        assert last_event.status.state == TaskState.failed
        assert last_event.final is True


# ---------------------------------------------------------------------------
# Gateway server tests
# ---------------------------------------------------------------------------

class TestCreateGatewayAppWithExplicitAgents:
    """Pass agent_names explicitly, verify app structure."""

    def test_creates_app_with_routes(self):
        with patch("krewcli.a2a.gateway_server.shutil.which", return_value="/usr/bin/claude"):
            app, spawn_mgr, registered = create_gateway_app(
                host="127.0.0.1",
                port=9000,
                working_dir="/tmp/test",
                agent_names=["claude"],
            )

        assert registered == ["claude"]
        assert spawn_mgr is not None

        route_paths = [r.path for r in app.routes]
        assert "/agents/claude" in route_paths
        assert "/health" in route_paths


class TestGatewayAgentCardUrl:
    """build_gateway_agent_card produces correct URL and metadata."""

    def test_card_url_format(self):
        card = build_gateway_agent_card("claude", "127.0.0.1", 9000)

        assert card.url == "http://127.0.0.1:9000/agents/claude"
        assert card.name == "gateway:claude"
        assert len(card.skills) == 1
        assert card.skills[0].id == "gateway:claude"
        assert card.capabilities.streaming is False

    def test_card_for_unknown_agent_still_works(self):
        card = build_gateway_agent_card("unknown_agent", "localhost", 8080)

        assert card.url == "http://localhost:8080/agents/unknown_agent"
        assert card.name == "gateway:unknown_agent"


# ---------------------------------------------------------------------------
# SpawnManager recipe context tests
# ---------------------------------------------------------------------------

class TestSpawnManagerRecipeContexts:
    """Per-recipe context resolution and spawn-time overrides."""

    def test_resolve_known_recipe(self):
        contexts = {
            "recipe-alpha": {
                "working_dir": "/tmp/alpha",
                "repo_url": "http://example.com/alpha.git",
                "branch": "dev",
            },
        }
        mgr = SpawnManager(working_dir="/tmp/default", recipe_contexts=contexts)
        ctx = mgr.resolve_recipe_context("recipe-alpha")
        assert ctx["working_dir"] == "/tmp/alpha"
        assert ctx["repo_url"] == "http://example.com/alpha.git"
        assert ctx["branch"] == "dev"

    def test_resolve_unknown_recipe_falls_back(self):
        mgr = SpawnManager(
            working_dir="/tmp/default",
            repo_url="http://default.git",
            branch="main",
            recipe_contexts={"other": {"working_dir": "/tmp/other"}},
        )
        ctx = mgr.resolve_recipe_context("unknown")
        assert ctx["working_dir"] == "/tmp/default"
        assert ctx["repo_url"] == "http://default.git"
        assert ctx["branch"] == "main"

    @pytest.mark.asyncio
    async def test_spawn_passes_per_recipe_context(self):
        mock_agent = _make_mock_agent()

        with patch("krewcli.a2a.spawn_manager.get_agent", return_value=mock_agent):
            mgr = SpawnManager(working_dir="/tmp/default")
            started = await mgr.spawn(
                "claude", "a-1", "t-1", "prompt",
                working_dir="/tmp/recipe-x",
                repo_url="http://recipe-x.git",
                branch="feature",
            )
            assert started is True
            await asyncio.sleep(0.05)

            # Agent was called with per-recipe deps
            call_kwargs = mock_agent.run.call_args.kwargs
            deps = call_kwargs["deps"]
            assert deps.working_dir == "/tmp/recipe-x"
            assert deps.repo_url == "http://recipe-x.git"
            assert deps.branch == "feature"


# ---------------------------------------------------------------------------
# GatewayExecutor recipe routing tests
# ---------------------------------------------------------------------------

class TestGatewayExecutorRecipeRouting:
    """Executor extracts recipe_name from metadata and passes context to spawn."""

    @pytest.mark.asyncio
    async def test_routes_recipe_context_to_spawn(self):
        mock_spawn = Mock(spec=SpawnManager)
        mock_spawn.running_count_for = Mock(return_value=0)
        mock_spawn.spawn = AsyncMock(return_value=True)
        mock_spawn.resolve_recipe_context = Mock(return_value={
            "working_dir": "/tmp/recipe-a",
            "repo_url": "http://recipe-a.git",
            "branch": "main",
        })

        executor = GatewayExecutor(
            agent_name="claude",
            spawn_manager=mock_spawn,
            agent_id="a-1",
        )

        ctx = _make_request_context(
            text="implement feature",
            metadata={"task_id": "t-1", "recipe_name": "recipe-a"},
        )
        eq = _make_event_queue()

        await executor.execute(ctx, eq)

        mock_spawn.resolve_recipe_context.assert_called_once_with("recipe-a")
        spawn_kwargs = mock_spawn.spawn.call_args.kwargs
        assert spawn_kwargs["working_dir"] == "/tmp/recipe-a"
        assert spawn_kwargs["repo_url"] == "http://recipe-a.git"

    @pytest.mark.asyncio
    async def test_no_recipe_name_passes_none(self):
        mock_spawn = Mock(spec=SpawnManager)
        mock_spawn.running_count_for = Mock(return_value=0)
        mock_spawn.spawn = AsyncMock(return_value=True)

        executor = GatewayExecutor(
            agent_name="claude",
            spawn_manager=mock_spawn,
            agent_id="a-1",
        )

        ctx = _make_request_context(
            text="implement feature",
            metadata={"task_id": "t-1"},
        )
        eq = _make_event_queue()

        await executor.execute(ctx, eq)

        # No recipe_name → resolve_recipe_context should NOT be called
        mock_spawn.resolve_recipe_context.assert_not_called()
        spawn_kwargs = mock_spawn.spawn.call_args.kwargs
        assert spawn_kwargs["working_dir"] is None


# ---------------------------------------------------------------------------
# Gateway server recipe_contexts tests
# ---------------------------------------------------------------------------

class TestCreateGatewayAppWithRecipeContexts:
    """recipe_contexts param is passed through to SpawnManager."""

    def test_recipe_contexts_passed_to_spawn_manager(self):
        contexts = {"recipe-a": {"working_dir": "/tmp/a"}}
        with patch("krewcli.a2a.gateway_server.shutil.which", return_value="/usr/bin/claude"):
            app, spawn_mgr, registered = create_gateway_app(
                host="127.0.0.1",
                port=9000,
                working_dir="/tmp/test",
                agent_names=["claude"],
                recipe_contexts=contexts,
            )

        assert spawn_mgr._recipe_contexts == contexts
        assert registered == ["claude"]
