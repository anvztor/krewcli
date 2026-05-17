"""Brain spawn must NOT fetch /credentials/envs and must NOT contain platform tokens.

Phase 0 auth redesign: credentials reach the SANDBOX JIT via
cookrew-beta → SPA → krewhub /credential-relay → SandboxHand.
The brain starts with operator shell env only.
"""
from __future__ import annotations

import asyncio
import os
import pytest

import httpx


# ---------------------------------------------------------------------------
# Verify merge_vault_envs_into is gone from execenv
# ---------------------------------------------------------------------------


def test_merge_vault_envs_into_removed_from_execenv():
    """merge_vault_envs_into must not exist on ExecutionEnvironment."""
    from krewcli.daemon.execenv import ExecutionEnvironment
    assert not hasattr(ExecutionEnvironment, "merge_vault_envs_into"), (
        "merge_vault_envs_into was not removed from ExecutionEnvironment — "
        "Phase 0 auth redesign requires its deletion"
    )


def test_fetch_vault_envs_removed_from_execenv():
    """fetch_vault_envs must not exist on ExecutionEnvironment."""
    from krewcli.daemon.execenv import ExecutionEnvironment
    assert not hasattr(ExecutionEnvironment, "fetch_vault_envs"), (
        "fetch_vault_envs was not removed from ExecutionEnvironment — "
        "Phase 0 auth redesign requires its deletion"
    )


# ---------------------------------------------------------------------------
# Verify backends no longer import / call merge_vault_envs_into
# ---------------------------------------------------------------------------


def test_claude_backend_does_not_call_merge_vault_envs(tmp_path, monkeypatch):
    """_run_claude must not call merge_vault_envs_into before spawning.

    We verify this by ensuring the execenv module has no such method
    and that importing/calling the backend doesn't trigger a /credentials/envs
    HTTP call.
    """
    import krewcli.backend.claude as claude_mod
    import inspect

    source = inspect.getsource(claude_mod._run_claude)
    assert "merge_vault_envs_into" not in source, (
        "claude._run_claude still calls merge_vault_envs_into — remove it"
    )
    assert "credentials/envs" not in source, (
        "claude._run_claude still references /credentials/envs — remove it"
    )


def test_codex_backend_does_not_call_merge_vault_envs():
    import krewcli.backend.codex as codex_mod
    import inspect

    source = inspect.getsource(codex_mod._run_codex)
    assert "merge_vault_envs_into" not in source, (
        "codex._run_codex still calls merge_vault_envs_into — remove it"
    )
    assert "credentials/envs" not in source, (
        "codex._run_codex still references /credentials/envs — remove it"
    )


def test_gemini_backend_does_not_call_merge_vault_envs():
    import krewcli.backend.gemini as gemini_mod
    import inspect

    source = inspect.getsource(gemini_mod._run_gemini)
    assert "merge_vault_envs_into" not in source, (
        "gemini._run_gemini still calls merge_vault_envs_into — remove it"
    )
    assert "credentials/envs" not in source, (
        "gemini._run_gemini still references /credentials/envs — remove it"
    )


# ---------------------------------------------------------------------------
# Verify execenv source no longer references /credentials/envs in any method
# ---------------------------------------------------------------------------


def test_execenv_source_has_no_credentials_envs_call():
    """execenv.py must not contain live /credentials/envs calls.

    The old merge_vault_envs_into and fetch_vault_envs both called
    this endpoint. Both are removed in Phase 0.
    """
    import krewcli.daemon.execenv as execenv_mod
    import inspect

    source = inspect.getsource(execenv_mod)
    # The comment tombstone is allowed but not an actual GET call.
    # We check that there is no active URL path — only the comment line is OK.
    lines_with_endpoint = [
        line for line in source.splitlines()
        if "/credentials/envs" in line and not line.strip().startswith("#")
    ]
    assert not lines_with_endpoint, (
        f"execenv.py still has live /credentials/envs references: {lines_with_endpoint}"
    )
