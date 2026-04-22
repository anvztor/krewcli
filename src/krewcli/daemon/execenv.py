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

from krewcli.gateway.worktree import WorktreeManager, is_worktree_isolation_enabled

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._base_dir = base_dir
        self._task_id = task_id
        self._bundle_id = bundle_id
        self._repo_url = repo_url
        self._branch = branch
        self._worktree_path: str | None = None
        self._worktree_mgr: WorktreeManager | None = None

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
        recipe_id: str = "",
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the subprocess environment overlay.

        Sets KREWHUB_* vars that the agent and its hooks can use
        to identify the current execution context.
        """
        env = {
            "KREWHUB_TASK_ID": self._task_id,
            "KREWHUB_BUNDLE_ID": self._bundle_id,
            "KREWHUB_RECIPE_ID": recipe_id,
            "KREWHUB_REPO_URL": self._repo_url,
            "KREWHUB_BRANCH": self._branch,
        }
        if extra:
            env.update(extra)
        return env

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

        except OSError:
            logger.debug(
                "execenv: failed to write .agent_context for task %s",
                self._task_id,
            )
