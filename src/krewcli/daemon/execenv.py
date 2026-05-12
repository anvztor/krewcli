"""Execution environment — isolated workdir for task execution.

Manages per-task working directories with optional git worktree
isolation. Writes ``.agent_context/`` files for the agent to consume.

Replaces the scattered workdir logic in gateway/task_executor.py
and a2a/spawn_manager.py with a clean, multica-inspired abstraction.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from krewcli.gateway.worktree import WorktreeManager, is_worktree_isolation_enabled

logger = logging.getLogger(__name__)


_DELEGATE_INSTRUCTIONS = """\
# Delegating work outside your reasoning context

The `delegate` tool exposed by the krewcli-bridge MCP server (registered
as `mcp__krewcli-bridge__delegate`) is your ONLY way to act outside the
model. It is the only tool you have — Bash, Read, Edit, etc. are
unavailable.

```
delegate({
  to: "sandbox" | "human" | "agent:<id>",
  input: <string-or-object>,
  schema: <optional-MCP-elicitation-subset-schema>,
  deadline_s: <optional-int, default 300>,
  label:   <optional-display-tag>
})
```

`to: "sandbox"` (bare, no id) routes to the e2b sandbox attached to
this bundle — the bridge auto-resolves the id from the spawn env.
You don't need to know the sandbox id; just say `to: "sandbox"`.

The tool returns a `ResultEnvelope`:
  `{action: "accept"|"decline"|"cancel"|"error", content?, reason?}`

Treat all four actions uniformly — failures are values, not exceptions.

**Do NOT use the built-in `AskUserQuestion` tool.** It errors out in
this environment because there is no local UI; the operator is on a
remote web client and is reachable only through `delegate(to="human")`.
"""


class ExecutionEnvironment:
    """Isolated execution environment for a single task.

    Handles:
      - Working directory resolution
      - Optional git worktree creation
      - .agent_context/ metadata injection
      - Subprocess environment overlay
      - Cleanup after execution
    """

    def __init__(
        self,
        base_dir: str,
        task_id: str,
        bundle_id: str,
        repo_url: str = "",
        branch: str = "",
        sandbox_id: str | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._task_id = task_id
        self._bundle_id = bundle_id
        self._repo_url = repo_url
        self._branch = branch
        # Auth track A2 — when set, the harness emits sandbox.attached
        # and (eventually) routes execution into the e2b sandbox via
        # the e2b SDK. For now this is a metadata pass-through.
        self._sandbox_id = sandbox_id
        self._worktree_path: str | None = None
        self._worktree_mgr: WorktreeManager | None = None

    @property
    def sandbox_id(self) -> str | None:
        return self._sandbox_id

    @property
    def working_dir(self) -> str:
        """The effective working directory for the agent."""
        return self._worktree_path or self._base_dir

    async def setup(
        self,
        task_title: str = "",
        task_description: str = "",
        prompt: str = "",
    ) -> str:
        """Prepare the execution environment.

        Creates a git worktree if isolation is enabled, writes
        ``.agent_context/`` metadata files, and returns the effective
        working directory.
        """
        if is_worktree_isolation_enabled():
            try:
                from krewcli.agents.code_refs import read_git_value
                baseline = await read_git_value(
                    ["git", "rev-parse", "HEAD"], self._base_dir,
                )
                if baseline:
                    self._worktree_mgr = WorktreeManager(self._base_dir)
                    self._worktree_path = await self._worktree_mgr.create_worktree(
                        baseline, self._bundle_id, self._task_id,
                    )
                    logger.info(
                        "execenv: worktree at %s for task %s",
                        self._worktree_path, self._task_id,
                    )
            except Exception:
                logger.warning(
                    "execenv: worktree creation failed for task %s, using base dir",
                    self._task_id,
                )

        workdir = self.working_dir
        self._write_agent_context(workdir, task_title, task_description, prompt)
        return workdir

    async def teardown(self) -> None:
        """Clean up the execution environment."""
        if self._worktree_path and self._worktree_mgr:
            try:
                await self._worktree_mgr.cleanup_worktree(
                    self._worktree_path, self._bundle_id, self._task_id,
                )
            except Exception:
                logger.warning(
                    "execenv: worktree cleanup failed for task %s",
                    self._task_id,
                )

    def build_env(
        self,
        cookbook_id: str = "",
        extra: dict[str, str] | None = None,
        *,
        krewhub_url: str = "",
        session_token: str = "",
        parent_tape_id: str = "",
    ) -> dict[str, str]:
        """Build the subprocess environment overlay.

        Sets KREWHUB_* vars that the agent and its hooks can use
        to identify the current execution context AND that the
        krewcli-bridge MCP server uses to call back to krewhub when
        the brain invokes `delegate(...)`.

        Without `KREWHUB_URL` + `KREWHUB_SESSION_TOKEN`, the bridge
        can't be wired into claude — the brain would silently lack a
        `delegate` tool and either hallucinate operator answers or
        give up.
        """
        env = {
            "KREWHUB_TASK_ID": self._task_id,
            "KREWHUB_BUNDLE_ID": self._bundle_id,
            "KREWHUB_COOKBOOK_ID": cookbook_id,
            "KREWHUB_REPO_URL": self._repo_url,
            "KREWHUB_BRANCH": self._branch,
        }
        # Surface the bundle's e2b sandbox id so the bridge MCP server can
        # auto-resolve `delegate(to: "sandbox", ...)` to the correct VM.
        # Without this the brain has to ask the operator what its sandbox
        # is — see krewcli-bridge `delegate` impl.
        if self._sandbox_id:
            env["KREWHUB_SANDBOX_ID"] = self._sandbox_id
        if krewhub_url:
            env["KREWHUB_URL"] = krewhub_url
        if session_token:
            env["KREWHUB_SESSION_TOKEN"] = session_token
        if parent_tape_id:
            env["KREWHUB_PARENT_TAPE_ID"] = parent_tape_id
        if extra:
            env.update(extra)
        return env

    @staticmethod
    async def merge_vault_envs_into(proc_env: dict[str, str]) -> None:
        """Merge vault credentials into `proc_env` IN PLACE.

        Reads `KREWHUB_URL` + `KREWHUB_SESSION_TOKEN` directly from
        proc_env to identify krewhub. Only fills env vars that aren't
        already set — operator's shell vars + explicit extra_env take
        precedence over the vault (so a per-run `GITHUB_TOKEN=...` shell
        override still works).

        Called by each backend's spawner (claude.py / codex.py /
        gemini.py) immediately before subprocess launch.
        """
        krewhub_url = proc_env.get("KREWHUB_URL", "")
        session_token = proc_env.get("KREWHUB_SESSION_TOKEN", "")
        if not krewhub_url or not session_token:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as cl:
                r = await cl.get(
                    f"{krewhub_url.rstrip('/')}/api/v1/credentials/envs",
                    headers={"Authorization": f"Bearer {session_token}"},
                )
            if r.status_code != 200:
                if r.status_code != 404:
                    logger.warning(
                        "merge_vault_envs: HTTP %s body=%s",
                        r.status_code, r.text[:200],
                    )
                return
            envs = r.json().get("envs") if r.headers.get("content-type", "").startswith("application/json") else None
            if not isinstance(envs, dict):
                return
            added: list[str] = []
            for k, v in envs.items():
                if not (isinstance(k, str) and isinstance(v, str)):
                    continue
                if not k.replace("_", "").isalnum():
                    continue
                if k in proc_env:
                    continue  # shell/extra wins
                proc_env[k] = v
                added.append(k)
            if added:
                logger.info(
                    "merge_vault_envs: injected %d keys: %s",
                    len(added), sorted(added),
                )
        except Exception as exc:
            logger.warning("merge_vault_envs failed: %s", exc)

    async def fetch_vault_envs(
        self,
        *,
        krewhub_url: str,
        session_token: str,
    ) -> dict[str, str]:
        """Fetch operator-stored credentials from krewhub's vault as env-vars.

        Returns {env_var_name: plaintext, ...} for the caller's account.
        Called from the backend spawner at brain-launch time so MCP
        subprocesses (mcp__github__*, etc.) inherit GITHUB_TOKEN,
        OPENAI_API_KEY, and similar without the brain ever holding them.

        Path B trade-off: plaintext lives briefly in the daemon's
        process memory and the brain's env. The brain's --allowed-tools
        whitelist + system note still prevent the brain from echoing
        these. Future Path A upgrade (HTTPS_PROXY broker) keeps the
        same surface but removes the env-injection step entirely.

        Best-effort: any failure (network, krewhub down, route 404 on
        an older krewhub) returns {} so spawn proceeds with whatever
        env the operator already has. The brain will still surface
        `op:auth_required` if it hits a Bad-credentials response, and
        the operator can paste / OAuth the missing token.
        """
        if not krewhub_url or not session_token:
            return {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as cl:
                r = await cl.get(
                    f"{krewhub_url.rstrip('/')}/api/v1/credentials/envs",
                    headers={"Authorization": f"Bearer {session_token}"},
                )
            if r.status_code == 404:
                # krewhub older than the vault-MVP rollout — fail open
                return {}
            if r.status_code != 200:
                logger.warning(
                    "execenv.fetch_vault_envs: HTTP %s (body=%s)",
                    r.status_code, r.text[:200],
                )
                return {}
            body = r.json()
            envs = body.get("envs") if isinstance(body, dict) else None
            if not isinstance(envs, dict):
                return {}
            # Filter to alphanumeric+underscore keys (defense-in-depth
            # against weird upstream output)
            return {
                k: v for k, v in envs.items()
                if isinstance(k, str) and isinstance(v, str)
                and k.replace("_", "").isalnum()
            }
        except Exception as exc:
            logger.warning(
                "execenv.fetch_vault_envs failed: %s", exc,
            )
            return {}

    def _write_agent_context(
        self,
        workdir: str,
        task_title: str,
        task_description: str,
        prompt: str,
    ) -> None:
        """Write .agent_context/ metadata for the agent to consume."""
        ctx_dir = Path(workdir) / ".agent_context"
        try:
            ctx_dir.mkdir(parents=True, exist_ok=True)

            task_meta = {
                "task_id": self._task_id,
                "bundle_id": self._bundle_id,
                "sandbox_id": self._sandbox_id,
                "title": task_title,
                "description": task_description,
                "repo_url": self._repo_url,
                "branch": self._branch,
            }
            (ctx_dir / "task.json").write_text(
                json.dumps(task_meta, indent=2), encoding="utf-8",
            )

            if prompt:
                (ctx_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

            # Slice 4 — surface the krewcli-bridge `delegate` tool to
            # the brain. Aligned with Anthropic Managed Agents'
            # `execute(name, input) -> string` primitive.
            (ctx_dir / "agent_instructions.md").write_text(
                _DELEGATE_INSTRUCTIONS, encoding="utf-8",
            )

        except OSError:
            logger.debug(
                "execenv: failed to write .agent_context for task %s",
                self._task_id,
            )
