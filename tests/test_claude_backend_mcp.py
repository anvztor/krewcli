"""Slice 4 — claude backend wires the bridge MCP server.

When the daemon launches `claude -p`, it must:
1. Build an `--mcp-config <path>` arg pointing to a generated JSON file.
2. The JSON file declares the krewcli-bridge stdio server with the
   right env vars (KREWHUB_URL, KREWHUB_TASK_ID, KREWHUB_SESSION_TOKEN,
   KREWHUB_PARENT_TAPE_ID).
3. `--allowedTools "mcp__krewcli-bridge__*"` is included so claude is
   permitted to call the bridge.

Status: RED.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_build_claude_args_includes_mcp_config(tmp_path):
    from krewcli.backend.claude import build_claude_args, write_mcp_config

    config_path = write_mcp_config(
        tmp_path,
        krewhub_url="http://krewhub:8420",
        task_id="task_1",
        session_token="tok_xyz",
        parent_tape_id="tape_parent_42",
        bundle_id="bun_99",
        recipe_id="rec_77",
    )
    args = build_claude_args(prompt="do the thing", mcp_config_path=config_path)

    assert "claude" in args[0]
    assert "--mcp-config" in args
    assert str(config_path) in args
    # AskUserQuestion is denied so the brain is forced to reach for
    # `delegate(to="human", ...)` instead.
    assert "--disallowedTools" in args
    disallow_idx = args.index("--disallowedTools")
    assert "AskUserQuestion" in args[disallow_idx + 1]
    assert "-p" in args
    assert "do the thing" in args


def test_write_mcp_config_emits_valid_json(tmp_path):
    from krewcli.backend.claude import write_mcp_config

    config_path = write_mcp_config(
        tmp_path,
        krewhub_url="http://krewhub:8420",
        task_id="task_1",
        session_token="tok_xyz",
        parent_tape_id="tape_p",
        bundle_id="bun_1",
        recipe_id="rec_1",
    )
    body = json.loads(Path(config_path).read_text())

    assert "mcpServers" in body
    assert "krewcli-bridge" in body["mcpServers"]
    server = body["mcpServers"]["krewcli-bridge"]
    cmd = server["command"]
    assert "python" in cmd or cmd == "uv", f"unexpected mcp command: {cmd!r}"
    # Must launch our module
    args_blob = " ".join(server.get("args", []))
    assert "krewcli.mcp_servers.bridge" in args_blob
    # Env vars surfaced
    env = server["env"]
    assert env["KREWHUB_URL"] == "http://krewhub:8420"
    assert env["KREWHUB_TASK_ID"] == "task_1"
    assert env["KREWHUB_SESSION_TOKEN"] == "tok_xyz"
    assert env["KREWHUB_PARENT_TAPE_ID"] == "tape_p"
    assert env["KREWHUB_BUNDLE_ID"] == "bun_1"
    assert env["KREWHUB_RECIPE_ID"] == "rec_1"


def test_write_mcp_config_default_path(tmp_path):
    """Same workdir → same config filename so subsequent claude runs in
    the same task reuse the file."""
    from krewcli.backend.claude import write_mcp_config

    p1 = write_mcp_config(
        tmp_path,
        krewhub_url="http://x", task_id="t1",
        session_token="s", parent_tape_id="p",
        bundle_id="b", recipe_id="r",
    )
    p2 = write_mcp_config(
        tmp_path,
        krewhub_url="http://x", task_id="t1",
        session_token="s", parent_tape_id="p",
        bundle_id="b", recipe_id="r",
    )
    assert p1 == p2


def test_execenv_appends_delegate_system_prompt_note(tmp_path):
    """`_write_agent_context` (or its caller) MUST append a note teaching
    the model to use `delegate` instead of AskUserQuestion."""
    from krewcli.daemon.execenv import ExecutionEnvironment

    env = ExecutionEnvironment(
        base_dir=str(tmp_path / "_base"),
        task_id="task_1",
        bundle_id="bun_1",
        repo_url="https://example.com/x.git",
        branch="main",
    )
    # Drive the private method directly; it's the existing API surface
    # that bundles agent context.
    env._write_agent_context(
        workdir=str(tmp_path),
        task_title="hello",
        task_description="say hi",
        prompt="please say hi to the operator",
    )
    # The bundle must include a hint about `delegate` somewhere a brain
    # would read it: prompt.txt or a new agent_instructions file.
    candidates = [
        tmp_path / ".agent_context" / "prompt.txt",
        tmp_path / ".agent_context" / "agent_instructions.md",
        tmp_path / ".agent_context" / "task.json",
    ]
    found = False
    for c in candidates:
        if c.exists():
            text = c.read_text()
            if "delegate" in text and "AskUserQuestion" in text:
                found = True
                break
    assert found, (
        "execenv must surface the 'use delegate, not AskUserQuestion' note "
        "to the brain via .agent_context/"
    )
