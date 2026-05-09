"""ExecutionEnvironment sandbox-id wiring (auth track A2)."""
from __future__ import annotations

from krewcli.daemon.execenv import ExecutionEnvironment


def test_execenv_records_sandbox_id(tmp_path):
    env = ExecutionEnvironment(
        base_dir=str(tmp_path),
        task_id="t1",
        bundle_id="b1",
        sandbox_id="sbx_abc",
    )
    assert env.sandbox_id == "sbx_abc"


def test_execenv_default_sandbox_id_is_none(tmp_path):
    env = ExecutionEnvironment(
        base_dir=str(tmp_path),
        task_id="t1",
        bundle_id="b1",
    )
    assert env.sandbox_id is None


def test_build_env_surfaces_sandbox_id_when_set(tmp_path):
    """The bridge MCP server reads KREWHUB_SANDBOX_ID to auto-resolve
    `delegate(to: "sandbox", ...)`. Without it, the brain has to ask the
    operator what its sandbox is — which is the failure mode that broke
    the cookrew-beta task on 2026-05-09."""
    env = ExecutionEnvironment(
        base_dir=str(tmp_path),
        task_id="t1",
        bundle_id="b1",
        sandbox_id="sbx_attached",
    )
    overlay = env.build_env(krewhub_url="http://krewhub:8420", session_token="tok")
    assert overlay.get("KREWHUB_SANDBOX_ID") == "sbx_attached"


def test_build_env_omits_sandbox_id_when_unset(tmp_path):
    """When the bundle has no sandbox the env var must be ABSENT, not
    empty — so the bridge raises a clear `no_sandbox_attached` error
    rather than POSTing `target=sandbox:` with a blank id."""
    env = ExecutionEnvironment(
        base_dir=str(tmp_path),
        task_id="t1",
        bundle_id="b1",
    )
    overlay = env.build_env(krewhub_url="http://krewhub:8420")
    assert "KREWHUB_SANDBOX_ID" not in overlay
