"""All-agents delegate wiring (Three Hands Protocol).

Mirrors `test_claude_backend_mcp.py` for codex + gemini. Each backend
must:
  1. Detect the KREWHUB_TASK_ID + KREWHUB_URL env signal.
  2. Materialize a per-task MCP config the CLI can pick up.
  3. Surface the krewcli-bridge `delegate` tool to the brain.
  4. Plant the delegate guidance in the brain's instructions so it
     reaches for `delegate(to: "human", ...)` instead of any built-in
     ask-user tool.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def test_codex_home_writer_creates_valid_toml(tmp_path: Path) -> None:
    from krewcli.backend._delegate import write_codex_home

    home = write_codex_home(
        tmp_path,
        krewhub_url="http://krewhub:8420",
        task_id="task_1",
        session_token="tok_xyz",
        parent_tape_id="tape_parent",
        bundle_id="bun_42",
        recipe_id="rec_77",
    )

    home_path = Path(home)
    assert home_path.exists()
    config_path = home_path / "config.toml"
    assert config_path.exists()

    text = config_path.read_text(encoding="utf-8")
    # Server section header — TOML allows hyphens in bare keys.
    assert "[mcp_servers.krewcli-bridge]" in text
    assert "command = " in text
    # All KREWHUB_* env vars surfaced.
    assert "KREWHUB_URL" in text
    assert "http://krewhub:8420" in text
    assert "KREWHUB_TASK_ID" in text
    assert "task_1" in text
    assert "KREWHUB_SESSION_TOKEN" in text
    assert "tok_xyz" in text
    assert "KREWHUB_PARENT_TAPE_ID" in text
    assert "tape_parent" in text
    # Bridge module is the right entrypoint.
    assert "krewcli.mcp_servers.bridge" in text


def test_codex_home_writer_uses_tomllib_compatible_syntax(tmp_path: Path) -> None:
    """Verify the TOML we emit actually parses. Catches escaping bugs."""
    import tomllib  # py3.11+

    from krewcli.backend._delegate import write_codex_home

    home = write_codex_home(
        tmp_path,
        krewhub_url='https://hub.cookrew.dev/path?with="quotes"',
        task_id="task_with\\backslash",
        session_token="tok",
        parent_tape_id="tap",
        bundle_id="bun",
        recipe_id="rec",
    )

    config_path = Path(home) / "config.toml"
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    bridge = parsed["mcp_servers"]["krewcli-bridge"]
    assert bridge["args"] == ["-m", "krewcli.mcp_servers.bridge"]
    # Special chars round-tripped intact.
    assert 'with="quotes"' in bridge["env"]["KREWHUB_URL"]
    assert "\\" in bridge["env"]["KREWHUB_TASK_ID"]


def test_codex_home_symlinks_auth_when_present(tmp_path: Path, monkeypatch) -> None:
    """codex stores its OAuth in `<CODEX_HOME>/auth.json`. The per-task
    home must inherit that file so codex doesn't fall through to the
    API-key (HTTP 401) path."""
    from krewcli.backend._delegate import write_codex_home

    real_home = tmp_path / "real_codex_home"
    real_home.mkdir()
    (real_home / "auth.json").write_text('{"access_token":"FAKE"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(real_home))

    workdir = tmp_path / "workdir"
    home = write_codex_home(
        workdir,
        krewhub_url="u",
        task_id="t",
        session_token="s",
        parent_tape_id="p",
        bundle_id="b",
        recipe_id="r",
    )
    auth_link = Path(home) / "auth.json"
    assert auth_link.exists()
    assert "FAKE" in auth_link.read_text(encoding="utf-8")


def test_gemini_settings_writer(tmp_path: Path) -> None:
    from krewcli.backend._delegate import write_gemini_settings

    settings_dir = write_gemini_settings(
        tmp_path,
        krewhub_url="http://krewhub:8420",
        task_id="task_g",
        session_token="tok",
        parent_tape_id="tap",
        bundle_id="bun",
        recipe_id="rec",
    )

    settings_path = Path(settings_dir) / "settings.json"
    assert settings_path.exists()
    body = json.loads(settings_path.read_text(encoding="utf-8"))
    bridge = body["mcpServers"]["krewcli-bridge"]
    assert bridge["args"] == ["-m", "krewcli.mcp_servers.bridge"]
    assert bridge["env"]["KREWHUB_URL"] == "http://krewhub:8420"
    assert bridge["env"]["KREWHUB_TASK_ID"] == "task_g"
    # Trust the bridge — we control it.
    assert bridge["trust"] is True


def test_gemini_settings_path_is_project_scope(tmp_path: Path) -> None:
    """Gemini reads project MCP config from `<cwd>/.gemini/settings.json`.
    Backend spawns gemini with cwd=working_dir, so the file must land
    inside the working dir, not in a sibling location."""
    from krewcli.backend._delegate import write_gemini_settings

    settings_dir = write_gemini_settings(
        tmp_path,
        krewhub_url="u", task_id="t", session_token="s",
        parent_tape_id="p", bundle_id="b", recipe_id="r",
    )
    assert Path(settings_dir) == tmp_path / ".gemini"


def test_delegate_preamble_contains_human_handoff_guidance() -> None:
    """The system note must explicitly forbid AskUserQuestion / codex's
    `request_user_input` and steer the brain to delegate(to: "human")."""
    from krewcli.backend._delegate import (
        DELEGATE_SYSTEM_NOTE,
        prepend_delegate_preamble,
    )

    note = DELEGATE_SYSTEM_NOTE
    assert 'delegate(to: "human"' in note
    assert "AskUserQuestion" in note
    assert "request_user_input" in note

    out = prepend_delegate_preamble("user task here")
    assert "user task here" in out
    assert "delegate" in out
    # Note appears BEFORE user prompt so the brain reads guidance first.
    assert out.index("delegate") < out.index("user task here")


def test_delegate_preamble_contains_sandbox_op_vocabulary() -> None:
    """Phase 3/4: the brain must learn the four sandbox ops so it picks
    structured file I/O over `cat <<EOF` shell hacks. Plan:
    docs/superpowers/plans/2026-05-08-sandbox-hand-vocabulary.md."""
    from krewcli.backend._delegate import DELEGATE_SYSTEM_NOTE

    note = DELEGATE_SYSTEM_NOTE
    # Each op kind must appear exactly as the brain will type it.
    assert 'op: "exec"' in note
    assert 'op: "write"' in note
    assert 'op: "read"' in note
    assert 'op: "list"' in note
    # Encoding hint for binary writes.
    assert "base64" in note
    # Backwards-compat statement: bare string input still means exec.
    assert "exec" in note.lower() and "string" in note.lower()


def test_delegate_preamble_promises_bare_sandbox_target() -> None:
    """Brain should not need to know its sandbox id — bare `to: "sandbox"`
    routes to the bundle's attached VM via bridge auto-resolution. The
    note must teach this explicitly so the brain doesn't ask the operator
    or fall back to local Bash (the failure mode the cookrew-beta task
    surfaced on 2026-05-09)."""
    from krewcli.backend._delegate import DELEGATE_SYSTEM_NOTE

    note = DELEGATE_SYSTEM_NOTE
    # The bare-form target: THE message that fixes the bug where the
    # brain asked the human "what's my sandbox id?".
    assert 'to: "sandbox"' in note
    # And brain should be told delegate is the only tool — closes the
    # local-Bash escape hatch in the note layer (the actual lock-down
    # is via --allowed-tools in build_claude_args).
    note_lower = note.lower()
    assert "only" in note_lower and "delegate" in note_lower


def test_bridge_env_includes_sandbox_id_when_set(tmp_path: Path) -> None:
    """write_claude_mcp_config must surface KREWHUB_SANDBOX_ID to the
    bridge subprocess so it can auto-resolve `to: "sandbox"`."""
    from krewcli.backend._delegate import write_claude_mcp_config

    config_path = write_claude_mcp_config(
        tmp_path,
        krewhub_url="http://krewhub:8420",
        task_id="task_1",
        session_token="tok",
        parent_tape_id="tape",
        bundle_id="bun_42",
        recipe_id="rec_99",
        sandbox_id="sbx_attached_to_bundle",
    )
    body = json.loads(Path(config_path).read_text(encoding="utf-8"))
    bridge_env = body["mcpServers"]["krewcli-bridge"]["env"]
    assert bridge_env.get("KREWHUB_SANDBOX_ID") == "sbx_attached_to_bundle"


def test_bridge_env_omits_sandbox_id_when_unset(tmp_path: Path) -> None:
    """When the bundle has no sandbox, the env var should be absent
    rather than empty so the bridge surfaces a clear no_sandbox_attached
    error instead of trying to call krewhub with an empty id."""
    from krewcli.backend._delegate import write_claude_mcp_config

    config_path = write_claude_mcp_config(
        tmp_path,
        krewhub_url="http://krewhub:8420",
        task_id="task_1",
        session_token="tok",
        parent_tape_id="tape",
        bundle_id="bun_42",
        recipe_id="rec_99",
    )
    body = json.loads(Path(config_path).read_text(encoding="utf-8"))
    bridge_env = body["mcpServers"]["krewcli-bridge"]["env"]
    assert "KREWHUB_SANDBOX_ID" not in bridge_env


def test_delegate_wiring_active_predicate() -> None:
    from krewcli.backend._delegate import delegate_wiring_active

    assert not delegate_wiring_active(None)
    assert not delegate_wiring_active({})
    assert not delegate_wiring_active({"KREWHUB_URL": "u"})
    assert not delegate_wiring_active({"KREWHUB_TASK_ID": "t"})
    assert delegate_wiring_active(
        {"KREWHUB_URL": "u", "KREWHUB_TASK_ID": "t"}
    )


def test_bridge_command_is_current_python() -> None:
    """The bridge MCP server is invoked as `<sys.executable> -m krewcli.mcp_servers.bridge`.
    A wrong python here means the daemon's venv loses krewcli imports."""
    from krewcli.backend._delegate import write_claude_mcp_config
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = write_claude_mcp_config(
            td,
            krewhub_url="u", task_id="t", session_token="s",
            parent_tape_id="p", bundle_id="b", recipe_id="r",
        )
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        assert body["mcpServers"]["krewcli-bridge"]["command"] == sys.executable
