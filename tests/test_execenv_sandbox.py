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
