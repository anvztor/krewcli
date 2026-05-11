"""Shared delegate-wiring helpers for all CLI backends.

Each backend (claude, codex, gemini) needs the same three things wired
when KREWHUB_TASK_ID + KREWHUB_URL are present in the spawn env:

1. A stdio MCP server entry called `krewcli-bridge` that exposes the
   `delegate(target, input, ...)` tool. The actual server lives at
   `krewcli.mcp_servers.bridge`.
2. The KREWHUB_* env vars surfaced to that MCP server so it can call
   `/api/v1/invocations` on the user's behalf.
3. A short system-prompt note instructing the brain to use `delegate`
   instead of any built-in user-question tool. Headless `<cli> -p`
   has no local UI, so AskUserQuestion-style tools error out.

Backends differ on *how* they accept those three knobs (claude has
--mcp-config + --append-system-prompt; codex reads ~/.codex/config.toml
and has no system-prompt flag; gemini reads .gemini/settings.json
relative to cwd and has --allowed-mcp-server-names). This module owns
the file-writing and shape; the per-backend module wires the rest.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# System note injected into the brain's instructions
# ---------------------------------------------------------------------------


DELEGATE_SYSTEM_NOTE = """\
You are running headlessly under krewcli. Your bundle has an attached \
e2b sandbox — use it for all file, exec, and code operations. Your only \
external tool is `delegate` (exposed as `mcp__krewcli-bridge__delegate`); \
Bash, Read, Edit, Write, Glob, Grep, WebFetch, etc. are NOT available, \
and any task that requires them MUST be expressed via `delegate(to: \
"sandbox", ...)`.

  delegate({
    to: "sandbox" | "human" | "agent:<id>",
    input: <string-or-object>,
    schema?: <MCP-elicitation-subset-schema>,
    deadline_s?: 300,
    label?: <short-tag>
  })
  → ResultEnvelope { action: "accept"|"decline"|"cancel"|"error",
                     content?, reason? }

`to: "sandbox"` (bare, no id) routes to the e2b sandbox attached to \
your bundle. The platform handles substrate lifecycle end-to-end — \
the sandbox is provisioned (or re-provisioned, if it died) automatically \
on every call, so you can rely on `to: "sandbox"` always working. You \
NEVER need to know a sandbox id, ask the operator about sandbox state, \
or surface "no sandbox" issues — those are platform concerns, not your \
concern. To target a specific sandbox you already know about by id, \
use `to: "sandbox:<sbx_id>"`.

When a task asks you to ask, query, request input from, or otherwise \
involve the human operator, call `delegate(to: "human", input: <question>, \
schema: <optional schema>)`. This is the only way to reach the operator; \
there is no local UI. Failures are values — `delegate` always returns a \
ResultEnvelope, never raises.

For sandbox targets, `input` is an object with an `op` field that picks \
the operation to dispatch:

  {op: "exec",  command: "<sh -c command>", cwd?: "<path>", env?: {...}}
  {op: "write", path: "/abs/path", data: "<text|base64>", encoding?: "utf-8"|"base64"}
  {op: "read",  path: "/abs/path"}
  {op: "list",  path: "/abs/path", depth?: 1}

When `op` is omitted (or `input` is a bare string), "exec" is assumed and \
the string is run via `/bin/sh -c`. Binary file content MUST be base64 — \
set `encoding: "base64"` on writes and decode `content.data` accordingly \
when `content.encoding == "base64"` on reads. The MVP cap on a single \
write is 1 MiB; for larger payloads, fetch from inside the sandbox \
(e.g. `op: "exec"` with `curl` or `git clone`). Prefer `op: "exec"` for \
shell pipelines and let `git`, `ls`, `find`, `diff` do their jobs — \
file ops are best for binary, structured I/O, and pulling artifacts back.

Do NOT use any of these built-in tools — they have no UI in this \
environment and will time out: `AskUserQuestion`, `request_user_input`, \
`request_user_question`. Always route human-facing prompts through \
`delegate(to: "human", ...)` instead.

CREDENTIALS: When a tool call (git push, mcp__github__*, curl, etc.) \
returns an authentication-shaped failure — HTTP 401/403, "Bad \
credentials", "Authentication Failed", MCP error -32603, "authentication \
required", "invalid token", "permission denied" while talking to an \
upstream API — do NOT ask the operator for a token in plain text. \
Instead, surface the auth need as a STRUCTURED human delegate:

  delegate({
    to: "human",
    input: {
      op: "auth_required",
      host: "<upstream-host>",          // e.g. "api.github.com"
      env_var_name: "<conventional>",   // e.g. "GITHUB_TOKEN", "OPENAI_API_KEY"
      reason: "<what you were trying to do>"
    }
  })

The platform renders this as a typed Auth card with "Connect via \
GitHub" (OAuth) and paste-token fallback; the operator's credential \
is stored in cookrew's vault and injected as an env var on subsequent \
op:exec calls. On `action:"accept"` from this delegate, retry the \
failed operation — the credential is now available. NEVER print \
credentials to stdout, embed them in commit messages, or quote them \
back in your response.

CRITICAL — auth-failure response rules:
  • Your FIRST and ONLY response to an auth failure is the \
    delegate(to:"human", input:{op:"auth_required", ...}) call.
  • Do NOT present numbered "options 1/2/3", "switch to option N", \
    "I draft you commit", "reply 'ready' and I'll retry", or any \
    other menu of paths-forward to the operator. Those are anti-patterns.
  • Do NOT name the env var the operator should set ("usually a \
    GITHUB_PERSONAL_ACCESS_TOKEN env var..."). The vault handles env \
    var naming; you only pass `env_var_name` to the structured delegate.
  • If a SECOND auth failure happens after the operator authenticated \
    once, surface it the SAME way — another op:auth_required, NOT a \
    "still doesn't work, here's what to do" message. Likely cause: \
    the stored credential lacks the right scope (e.g. PAT lacks `repo`).
  • Sandbox lifecycle (502 / "sandbox not found") is NOT an auth \
    failure — that's substrate, handled by the platform. Just retry; \
    if it persists, surface a generic delegate(to:"human") asking \
    about the task (not the sandbox state).\
"""


# ---------------------------------------------------------------------------
# MCP-server descriptor (consumed by all three writers below)
# ---------------------------------------------------------------------------


def _bridge_env(
    *,
    krewhub_url: str,
    task_id: str,
    session_token: str,
    parent_tape_id: str,
    bundle_id: str,
    recipe_id: str,
    sandbox_id: str = "",
) -> dict[str, str]:
    env = {
        "KREWHUB_URL": krewhub_url,
        "KREWHUB_SESSION_TOKEN": session_token,
        "KREWHUB_TASK_ID": task_id,
        "KREWHUB_BUNDLE_ID": bundle_id,
        "KREWHUB_RECIPE_ID": recipe_id,
        "KREWHUB_PARENT_TAPE_ID": parent_tape_id,
    }
    # The bridge reads KREWHUB_SANDBOX_ID to auto-resolve bare
    # `delegate(to: "sandbox", ...)`; without it that target errors with
    # `no_sandbox_attached`.
    if sandbox_id:
        env["KREWHUB_SANDBOX_ID"] = sandbox_id
    return env


def _bridge_command_args() -> tuple[str, list[str]]:
    return sys.executable or "python", ["-m", "krewcli.mcp_servers.bridge"]


# ---------------------------------------------------------------------------
# Writers (one per CLI flavor)
# ---------------------------------------------------------------------------


def write_claude_mcp_config(
    workdir: str | Path,
    *,
    krewhub_url: str,
    task_id: str,
    session_token: str,
    parent_tape_id: str,
    bundle_id: str,
    recipe_id: str,
    sandbox_id: str = "",
) -> str:
    """Generate the JSON `--mcp-config` file claude expects.

    Same workdir + same task → same file path → idempotent.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = workdir / ".krewcli" / "claude_mcp_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    cmd, args = _bridge_command_args()
    body = {
        "mcpServers": {
            "krewcli-bridge": {
                "command": cmd,
                "args": args,
                "env": _bridge_env(
                    krewhub_url=krewhub_url,
                    task_id=task_id,
                    session_token=session_token,
                    parent_tape_id=parent_tape_id,
                    bundle_id=bundle_id,
                    recipe_id=recipe_id,
                    sandbox_id=sandbox_id,
                ),
            }
        }
    }
    config_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return str(config_path)


def write_codex_home(
    workdir: str | Path,
    *,
    krewhub_url: str,
    task_id: str,
    session_token: str,
    parent_tape_id: str,
    bundle_id: str,
    recipe_id: str,
    sandbox_id: str = "",
) -> str:
    """Build a per-task `CODEX_HOME` and write `config.toml` declaring
    the krewcli-bridge MCP server.

    codex reads `${CODEX_HOME:-~/.codex}/config.toml`. Per-task isolation
    keeps stale KREWHUB_* env from prior runs out of this codex session.
    Returns the absolute path to set as CODEX_HOME.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    home = workdir / ".krewcli" / "codex_home"
    home.mkdir(parents=True, exist_ok=True)
    # codex's session-state subdirs are created lazily; we just need
    # config.toml on disk before invoking codex.
    config_path = home / "config.toml"

    # codex stores OAuth credentials in `<CODEX_HOME>/auth.json`. By
    # using a per-task CODEX_HOME we'd otherwise lose the user's login
    # and codex would fall through to API-key path (HTTP 401). Symlink
    # auth.json from the user's *real* CODEX_HOME so the per-task home
    # inherits the existing login. Best-effort — failure is non-fatal,
    # codex will surface its own auth error if the link is missing.
    real_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    real_auth = real_home / "auth.json"
    link_target = home / "auth.json"
    if real_auth.exists() and not link_target.exists():
        try:
            link_target.symlink_to(real_auth.resolve())
        except OSError:
            try:
                link_target.write_text(
                    real_auth.read_text(encoding="utf-8"), encoding="utf-8",
                )
            except OSError:
                pass

    cmd, args = _bridge_command_args()
    env = _bridge_env(
        krewhub_url=krewhub_url,
        task_id=task_id,
        session_token=session_token,
        parent_tape_id=parent_tape_id,
        bundle_id=bundle_id,
        recipe_id=recipe_id,
        sandbox_id=sandbox_id,
    )

    lines: list[str] = []
    lines.append("# krewcli per-task codex config — do not edit")
    lines.append("[mcp_servers.krewcli-bridge]")
    lines.append(f'command = {_toml_str(cmd)}')
    lines.append(f"args = {_toml_array(args)}")
    lines.append(f"env = {_toml_inline_table(env)}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(home)


def write_gemini_settings(
    workdir: str | Path,
    *,
    krewhub_url: str,
    task_id: str,
    session_token: str,
    parent_tape_id: str,
    bundle_id: str,
    recipe_id: str,
    sandbox_id: str = "",
) -> str:
    """Write `.gemini/settings.json` (project scope) declaring the
    krewcli-bridge MCP server. Returns the path to the settings dir.

    Gemini's project settings live at `<cwd>/.gemini/settings.json`. The
    backend spawns gemini with `cwd=working_dir`, so writing this file
    inside the working dir is enough to register the server. We still
    pair it with `--allowed-mcp-server-names krewcli-bridge` so the CLI
    surfaces the server's tools even with the default policy.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    settings_dir = workdir / ".gemini"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    cmd, args = _bridge_command_args()
    body = {
        "mcpServers": {
            "krewcli-bridge": {
                "command": cmd,
                "args": args,
                "env": _bridge_env(
                    krewhub_url=krewhub_url,
                    task_id=task_id,
                    session_token=session_token,
                    parent_tape_id=parent_tape_id,
                    bundle_id=bundle_id,
                    recipe_id=recipe_id,
                    sandbox_id=sandbox_id,
                ),
                "trust": True,
            }
        }
    }
    settings_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return str(settings_dir)


# ---------------------------------------------------------------------------
# Prompt prefix (used by codex + gemini, which lack a system-prompt flag)
# ---------------------------------------------------------------------------


_PREAMBLE_DELIM = "── KREWCLI DELEGATE PROTOCOL ────────────────────"


def prepend_delegate_preamble(prompt: str) -> str:
    """Return `<delegate note>\\n\\n<delim>\\n\\n<prompt>`.

    Codex and Gemini headless modes don't expose a system-prompt flag,
    so the only place to plant the delegate guidance is the prompt
    itself. We use a clear delimiter so a brain that wants to echo the
    user's request can do so cleanly.
    """
    return f"{DELEGATE_SYSTEM_NOTE}\n\n{_PREAMBLE_DELIM}\n\n{prompt}"


# ---------------------------------------------------------------------------
# Helper: detect when delegate wiring should fire
# ---------------------------------------------------------------------------


def delegate_wiring_active(env: dict[str, str] | None) -> bool:
    """Backends call this with their resolved spawn env. When false,
    backends spawn the CLI with no MCP/system-note customization."""
    if not env:
        return False
    return bool(env.get("KREWHUB_TASK_ID") and env.get("KREWHUB_URL"))


# ---------------------------------------------------------------------------
# Tiny TOML emitters (only what the codex config needs).
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    """Escape a string as a TOML basic string."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _toml_inline_table(table: dict[str, str]) -> str:
    parts = [f"{_toml_key(k)} = {_toml_str(v)}" for k, v in table.items()]
    return "{ " + ", ".join(parts) + " }"


def _toml_key(key: str) -> str:
    if all(c.isalnum() or c in ("_", "-") for c in key):
        return key
    return _toml_str(key)


# Re-exports for backwards compatibility with claude.py imports.
__all__ = [
    "DELEGATE_SYSTEM_NOTE",
    "write_claude_mcp_config",
    "write_codex_home",
    "write_gemini_settings",
    "prepend_delegate_preamble",
    "delegate_wiring_active",
]
