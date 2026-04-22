"""Tests for the cli package structure introduced by the refactor.

Validates that the cli/ package __init__.py properly:
- Re-exports symbols for backward compatibility
- Registers all command modules
- Wires the _KrewCLI group error handling
- Makes submodule functions importable from the package root
"""

from __future__ import annotations

import click
import pytest


class TestCliPackageReExports:
    """Verify backward-compatible re-exports from krewcli.cli."""

    def test_main_is_click_group(self):
        from krewcli.cli import main
        assert isinstance(main, click.Group)

    def test_heartbeat_loop_re_exported(self):
        from krewcli.cli import HeartbeatLoop
        from krewcli.presence.heartbeat import HeartbeatLoop as _Original
        assert HeartbeatLoop is _Original

    def test_task_runner_re_exported(self):
        from krewcli.cli import TaskRunner
        from krewcli.workflow.task_runner import TaskRunner as _Original
        assert TaskRunner is _Original

    def test_krewhub_client_re_exported(self):
        from krewcli.cli import KrewHubClient
        from krewcli.client.krewhub_client import KrewHubClient as _Original
        assert KrewHubClient is _Original

    def test_get_settings_re_exported(self):
        from krewcli.cli import get_settings
        from krewcli.config import get_settings as _Original
        assert get_settings is _Original

    def test_httpx_re_exported(self):
        import httpx as _httpx
        from krewcli.cli import httpx as _re
        assert _re is _httpx

    def test_resolve_mode_re_exported(self):
        from krewcli.cli import _resolve_mode
        from krewcli.cli.join import _resolve_mode as _Original
        assert _resolve_mode is _Original

    def test_default_model_re_exported(self):
        from krewcli.cli import _default_model
        from krewcli.cli.join import _default_model as _Original
        assert _default_model is _Original

    def test_run_agent_re_exported(self):
        from krewcli.cli import _run_agent
        from krewcli.cli.join import _run_agent as _Original
        assert _run_agent is _Original

    def test_run_gateway_re_exported(self):
        from krewcli.cli import _run_gateway
        from krewcli.cli.join import _run_gateway as _Original
        assert _run_gateway is _Original

    def test_load_recipe_context_re_exported(self):
        from krewcli.cli import _load_recipe_context
        from krewcli.cli.claim import _load_recipe_context as _Original
        assert _load_recipe_context is _Original

    def test_run_task_worker_re_exported(self):
        from krewcli.cli import _run_task_worker
        from krewcli.cli.tasks import _run_task_worker as _Original
        assert _run_task_worker is _Original

    def test_run_task_worker_once_re_exported(self):
        from krewcli.cli import _run_task_worker_once
        from krewcli.cli.tasks import _run_task_worker_once as _Original
        assert _run_task_worker_once is _Original

    def test_gateway_agent_metadata_re_exported(self):
        from krewcli.cli import _gateway_agent_metadata
        from krewcli.gateway.identity import _gateway_agent_metadata as _Original
        assert _gateway_agent_metadata is _Original


class TestCliPackageAllExports:
    """The __all__ list covers every re-exported name."""

    def test_all_exports_are_importable(self):
        import krewcli.cli as cli_mod
        for name in cli_mod.__all__:
            assert hasattr(cli_mod, name), f"{name} in __all__ but not importable"


class TestCommandRegistration:
    """Verify all expected commands are registered on the main group."""

    def test_join_command_registered(self):
        from krewcli.cli import main
        assert "join" in main.commands

    def test_start_command_registered(self):
        from krewcli.cli import main
        assert "start" in main.commands

    def test_claim_command_registered(self):
        from krewcli.cli import main
        assert "claim" in main.commands

    def test_list_tasks_command_registered(self):
        from krewcli.cli import main
        assert "list-tasks" in main.commands

    def test_milestone_command_registered(self):
        from krewcli.cli import main
        assert "milestone" in main.commands

    def test_status_command_registered(self):
        from krewcli.cli import main
        assert "status" in main.commands

    def test_repo_diagram_command_registered(self):
        from krewcli.cli import main
        assert "repo-diagram" in main.commands


class TestKrewCLIGroupClass:
    """The _KrewCLI group class is the custom error-handling group."""

    def test_main_uses_krew_cli_group(self):
        from krewcli.cli import main, _KrewCLI
        assert isinstance(main, _KrewCLI)

    def test_krew_cli_is_click_group(self):
        from krewcli.cli import _KrewCLI
        assert issubclass(_KrewCLI, click.Group)

    def test_usage_error_is_re_raised(self, monkeypatch):
        from krewcli.cli import _KrewCLI

        group = _KrewCLI("krew")
        ctx = click.Context(group)

        def _raise_usage(self, context):
            raise click.UsageError("bad flags")

        monkeypatch.setattr(click.Group, "invoke", _raise_usage)

        with pytest.raises(click.UsageError, match="bad flags"):
            group.invoke(ctx)


class TestCliSubmoduleDirectImports:
    """Submodule functions can be imported directly from their home module."""

    def test_resolve_mode_from_join(self):
        from krewcli.cli.join import _resolve_mode
        assert callable(_resolve_mode)

    def test_default_model_from_join(self):
        from krewcli.cli.join import _default_model
        assert callable(_default_model)

    def test_load_recipe_context_from_claim(self):
        from krewcli.cli.claim import _load_recipe_context
        assert callable(_load_recipe_context)

    def test_run_task_worker_from_tasks(self):
        from krewcli.cli.tasks import _run_task_worker
        assert callable(_run_task_worker)

    def test_run_task_worker_once_from_tasks(self):
        from krewcli.cli.tasks import _run_task_worker_once
        assert callable(_run_task_worker_once)

    def test_register_functions_exist(self):
        from krewcli.cli.join import register_join_commands
        from krewcli.cli.claim import register_claim_commands
        from krewcli.cli.tasks import register_task_commands
        from krewcli.cli.gateway_cmds import register_gateway_commands

        for fn in (register_join_commands, register_claim_commands, register_task_commands, register_gateway_commands):
            assert callable(fn)
