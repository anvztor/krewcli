"""Integration-style tests for the krewcli CLI commands.

These tests drive the click command tree end-to-end via ``CliRunner`` and
substitute a fake ``KrewHubClient`` (the only network dependency reached
from the main group). They cover:

- ``list-tasks``: bundle/task formatting, empty list, status icon mapping
- ``milestone``: fact wiring + event POST
- ``claim``: success path, "no result" path, missing-recipe argument
- ``status``: agent registry rendering
- ``start`` (legacy): delegation to ``join``
- Group-level error handling for httpx ConnectError / HTTPStatusError /
  RequestError, including the SSL hint and 401 hint
"""

from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner

from krewcli.agents.models import TaskResult
from krewcli.cli import main


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal stand-in for KrewHubClient used by CLI command tests.

    Records the calls made by the command so tests can assert wiring.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.closed = False
        self.tasks_response: list[dict] = []
        self.events_posted: list[dict] = []
        self.list_tasks_calls: list[tuple] = []
        self._event_id = "evt_fake"

    async def list_tasks(self, recipe_id, bundle_statuses=None):
        self.list_tasks_calls.append((recipe_id, bundle_statuses))
        return self.tasks_response

    async def post_event(self, task_id, event_type, actor_id, body, facts=None, **kwargs):
        record = {
            "task_id": task_id,
            "type": event_type,
            "actor_id": actor_id,
            "body": body,
            "facts": list(facts or []),
        }
        self.events_posted.append(record)
        return {"id": self._event_id, **record}

    async def close(self):
        self.closed = True


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatusCommand:
    def test_lists_every_registered_agent_with_capabilities(self, runner):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        for name in ("claude", "codex", "bub"):
            assert name in result.output
        # Capability label should render at least once.
        assert "capabilities:" in result.output


# ---------------------------------------------------------------------------
# list-tasks
# ---------------------------------------------------------------------------


class TestListTasksCommand:
    def test_requires_recipe_option(self, runner):
        result = runner.invoke(main, ["list-tasks"])
        assert result.exit_code != 0
        assert "--recipe" in result.output

    def test_renders_tasks_grouped_by_bundle(self, runner, monkeypatch):
        client = _FakeClient()
        client.tasks_response = [
            {
                "id": "task_a",
                "title": "Wire up endpoint",
                "bundle_id": "bun_1",
                "bundle_status": "open",
                "bundle_prompt": "Build the heartbeat endpoint",
                "status": "open",
            },
            {
                "id": "task_b",
                "title": "Add tests",
                "bundle_id": "bun_1",
                "bundle_status": "open",
                "bundle_prompt": "Build the heartbeat endpoint",
                "status": "claimed",
                "claimed_by_agent_id": "claude_42",
            },
            {
                "id": "task_c",
                "title": "Document",
                "bundle_id": "bun_2",
                "bundle_status": "claimed",
                "bundle_prompt": "Update docs",
                "status": "blocked",
            },
        ]
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: client)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])

        assert result.exit_code == 0, result.output
        assert "Bundle: bun_1 [open]" in result.output
        assert "Bundle: bun_2 [claimed]" in result.output
        # Bundle header should appear ONCE per bundle even with multiple tasks.
        assert result.output.count("Bundle: bun_1") == 1
        # Status icon mapping
        assert "[ ] task_a: Wire up endpoint" in result.output
        assert "[>] task_b: Add tests (claude_42)" in result.output
        assert "[!] task_c: Document" in result.output
        # Bundle prompt should be echoed (truncated to 80 chars by the CLI).
        assert "Build the heartbeat endpoint" in result.output
        # Filter is forwarded to client
        assert client.list_tasks_calls == [("rec_1", ("open", "claimed"))]
        assert client.closed is True

    def test_unknown_status_uses_question_mark_icon(self, runner, monkeypatch):
        client = _FakeClient()
        client.tasks_response = [
            {
                "id": "task_x",
                "title": "Mystery",
                "bundle_id": "bun_99",
                "bundle_status": "open",
                "bundle_prompt": "Unknown",
                "status": "weird-state-not-in-map",
            },
        ]
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: client)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])

        assert result.exit_code == 0, result.output
        assert "[?] task_x: Mystery" in result.output

    def test_empty_response_still_closes_client(self, runner, monkeypatch):
        client = _FakeClient()  # default tasks_response = []
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: client)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_empty"])

        assert result.exit_code == 0, result.output
        # No bundle header rendered
        assert "Bundle:" not in result.output
        assert client.closed is True


# ---------------------------------------------------------------------------
# milestone
# ---------------------------------------------------------------------------


class TestMilestoneCommand:
    def test_posts_event_with_facts_indexed(self, runner, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: client)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(
            main,
            [
                "milestone", "task_77",
                "--body", "Step done",
                "--fact", "First claim",
                "--fact", "Second claim",
                "--agent-id", "agent_alpha",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Milestone posted: evt_fake" in result.output

        assert len(client.events_posted) == 1
        ev = client.events_posted[0]
        assert ev["task_id"] == "task_77"
        assert ev["type"] == "milestone"
        assert ev["actor_id"] == "agent_alpha"
        assert ev["body"] == "Step done"
        assert ev["facts"] == [
            {"id": "f_0", "claim": "First claim", "captured_by": "agent_alpha"},
            {"id": "f_1", "claim": "Second claim", "captured_by": "agent_alpha"},
        ]
        assert client.closed is True

    def test_default_agent_id_is_cli_user(self, runner, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: client)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(main, ["milestone", "task_1", "--body", "Done"])

        assert result.exit_code == 0, result.output
        assert client.events_posted[0]["actor_id"] == "cli_user"
        assert client.events_posted[0]["facts"] == []

    def test_requires_body_option(self, runner):
        result = runner.invoke(main, ["milestone", "task_1"])
        assert result.exit_code != 0
        assert "--body" in result.output

    def test_requires_task_id_argument(self, runner):
        result = runner.invoke(main, ["milestone", "--body", "x"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


class TestClaimCommand:
    def _patch_runtime(self, monkeypatch, *, runner_result: TaskResult | None):
        class _FakeHeartbeat:
            def __init__(self, *args, **kwargs) -> None:
                self.current_task_id = None

            def start(self):
                return None

            async def stop(self):
                return None

        class _FakeRunner:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def claim_and_execute(self, task_id):
                return runner_result

        async def _fake_load_recipe_context(client, recipe_id):
            return "git@example.com:repo.git", "main"

        monkeypatch.setattr("krewcli.cli.HeartbeatLoop", _FakeHeartbeat)
        monkeypatch.setattr("krewcli.cli.TaskRunner", _FakeRunner)
        monkeypatch.setattr("krewcli.cli._load_recipe_context", _fake_load_recipe_context)
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: _FakeClient())
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

    def test_success_path_reports_completed(self, runner, monkeypatch):
        self._patch_runtime(
            monkeypatch,
            runner_result=TaskResult(summary="All green", success=True),
        )
        result = runner.invoke(main, ["claim", "task_1", "--recipe", "rec_1"])
        assert result.exit_code == 0, result.output
        assert "Task task_1 completed: All green" in result.output

    def test_none_result_reports_failure(self, runner, monkeypatch):
        self._patch_runtime(monkeypatch, runner_result=None)
        result = runner.invoke(main, ["claim", "task_1", "--recipe", "rec_1"])
        assert result.exit_code == 0, result.output
        assert "could not be claimed" in result.output

    def test_blocked_uses_summary_when_reason_missing(self, runner, monkeypatch):
        self._patch_runtime(
            monkeypatch,
            runner_result=TaskResult(summary="Stuck on auth", success=False),
        )
        result = runner.invoke(main, ["claim", "task_1", "--recipe", "rec_1"])
        assert result.exit_code == 0, result.output
        # blocked_reason is None, so summary is used.
        assert "Task task_1 blocked: Stuck on auth" in result.output

    def test_requires_recipe_option(self, runner):
        result = runner.invoke(main, ["claim", "task_1"])
        assert result.exit_code != 0
        assert "--recipe" in result.output


# ---------------------------------------------------------------------------
# Group-level error handling (httpx → ClickException)
# ---------------------------------------------------------------------------


class TestGroupErrorHandling:
    """The _KrewCLI group converts httpx errors into ClickExceptions.

    We trigger them by making list-tasks raise on the first await.
    """

    def _install_raising_client(self, monkeypatch, exc):
        class _RaisingClient(_FakeClient):
            async def list_tasks(self, *args, **kwargs):
                raise exc

        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: _RaisingClient())
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

    def test_connect_error_ssl_hint(self, runner, monkeypatch):
        exc = httpx.ConnectError(
            "boom\n[SSL: CERTIFICATE_VERIFY_FAILED] cert verify failed"
        )
        self._install_raising_client(monkeypatch, exc)
        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])
        assert result.exit_code != 0
        assert "SSL certificate error" in result.output
        assert "KREWCLI_VERIFY_SSL=false" in result.output

    def test_connect_error_generic(self, runner, monkeypatch):
        exc = httpx.ConnectError("connection refused")
        self._install_raising_client(monkeypatch, exc)
        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])
        assert result.exit_code != 0
        assert "Cannot connect to KrewHub" in result.output
        assert "connection refused" in result.output

    def test_http_status_401_suggests_login(self, runner, monkeypatch):
        request = httpx.Request("GET", "http://hub/api/v1/x")
        response = httpx.Response(401, request=request, text="unauth")
        exc = httpx.HTTPStatusError("unauth", request=request, response=response)
        self._install_raising_client(monkeypatch, exc)

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])
        assert result.exit_code != 0
        assert "Authentication failed (401)" in result.output
        assert "krewcli login" in result.output

    def test_http_status_other_includes_status_and_body(self, runner, monkeypatch):
        request = httpx.Request("GET", "http://hub/api/v1/x")
        response = httpx.Response(500, request=request, text="kaboom server error")
        exc = httpx.HTTPStatusError("server error", request=request, response=response)
        self._install_raising_client(monkeypatch, exc)

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])
        assert result.exit_code != 0
        assert "KrewHub returned 500" in result.output
        assert "kaboom server error" in result.output

    def test_request_error_renders_network_message(self, runner, monkeypatch):
        # RequestError is the base — pick a concrete non-Connect/non-Status one.
        request = httpx.Request("GET", "http://hub/api/v1/x")
        exc = httpx.ReadTimeout("read timed out", request=request)
        self._install_raising_client(monkeypatch, exc)

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])
        assert result.exit_code != 0
        assert "Network error" in result.output
        assert "read timed out" in result.output

    def test_unrelated_exception_is_not_swallowed(self, runner, monkeypatch):
        # Sanity check: errors outside the httpx hierarchy bubble up.
        self._install_raising_client(monkeypatch, RuntimeError("unexpected"))

        result = runner.invoke(main, ["list-tasks", "--recipe", "rec_1"])
        assert result.exit_code != 0
        # CliRunner captures the exception in result.exception
        assert isinstance(result.exception, RuntimeError)


# ---------------------------------------------------------------------------
# start (legacy) — delegates to join
# ---------------------------------------------------------------------------


class TestStartLegacyCommand:
    def test_start_invokes_join_with_same_arguments(self, runner, monkeypatch):
        captured = {}

        def _fake_join(*args, **kwargs):
            # ctx.invoke may or may not forward the click context positionally
            # depending on the command's pass_context wiring; capture both.
            captured["args"] = args
            captured["kwargs"] = kwargs

        # Replace the join callback so we can observe the dispatch.
        monkeypatch.setattr("krewcli.cli.join.callback", _fake_join)
        monkeypatch.setattr("krewcli.cli.KrewHubClient", lambda *a, **kw: _FakeClient())
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(
            main,
            [
                "start",
                "--recipe", "rec_1",
                "--cookbook", "cb_1",
                "--agent", "claude",
                "--agent-id", "id_1",
                "--port", "9100",
                "--workdir", "/tmp/work",
            ],
        )

        assert result.exit_code == 0, result.output
        kwargs = captured["kwargs"]
        assert kwargs["recipe"] == "rec_1"
        assert kwargs["cookbook"] == "cb_1"
        assert kwargs["agent"] == "claude"
        assert kwargs["agent_id"] == "id_1"
        assert kwargs["port"] == 9100
        assert kwargs["workdir"] == "/tmp/work"


# ---------------------------------------------------------------------------
# main group bootstrap — ensures KrewHubClient is wired with settings
# ---------------------------------------------------------------------------


class TestMainGroupBootstrap:
    def test_client_constructed_with_jwt_when_token_present(self, runner, monkeypatch):
        captured = {}

        def _factory(base_url, api_key, jwt_token=None, verify_ssl=True, **kwargs):
            captured.update(
                base_url=base_url,
                api_key=api_key,
                jwt_token=jwt_token,
                verify_ssl=verify_ssl,
            )
            return _FakeClient()

        monkeypatch.setattr("krewcli.cli.KrewHubClient", _factory)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: "jwt-abc")

        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert captured["jwt_token"] == "jwt-abc"
        assert isinstance(captured["base_url"], str)
        assert isinstance(captured["api_key"], str)
        assert isinstance(captured["verify_ssl"], bool)

    def test_client_constructed_without_jwt_when_no_token(self, runner, monkeypatch):
        captured = {}

        def _factory(base_url, api_key, jwt_token=None, **kwargs):
            captured["jwt_token"] = jwt_token
            return _FakeClient()

        monkeypatch.setattr("krewcli.cli.KrewHubClient", _factory)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert captured["jwt_token"] is None
