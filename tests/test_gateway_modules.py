"""Boundary tests for the refactored gateway module split."""

from __future__ import annotations

import krewcli.gateway as gateway
import krewcli.gateway_helpers as gateway_helpers
import krewcli.gateway_runtime as gateway_runtime
import krewcli.gateway_tasks as gateway_tasks


def test_gateway_helpers_boundary_reexports_helper_functions():
    assert gateway_helpers._gateway_agent_metadata is gateway._gateway_agent_metadata
    assert gateway_helpers._get_owner_label is gateway._get_owner_label
    assert gateway_helpers._make_agent_id is gateway._make_agent_id
    assert gateway_helpers.build_auth_service is gateway.build_auth_service
    assert gateway_helpers.load_recipe_context is gateway.load_recipe_context


def test_gateway_runtime_boundary_reexports_run_gateway():
    assert gateway_runtime.run_gateway is gateway.run_gateway


def test_gateway_tasks_boundary_reexports_task_handlers():
    assert gateway_tasks._handle_planner_task is gateway._handle_planner_task
    assert gateway_tasks._handle_regular_task is gateway._handle_regular_task
    assert gateway_tasks._push_fork_tape is gateway._push_fork_tape
