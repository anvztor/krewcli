"""Per-task git worktree isolation for the gateway.

Creates an isolated git worktree for each task execution so parallel
agents can't interfere with each other's file changes. After execution,
collects a full unified diff (CodeDiff) and cleans up the worktree.

Gated behind KREWCLI_WORKTREE_ISOLATION=1 — disabled by default.

Design from the Anthropic managed-agents pattern:
  sandbox = worktree (cheap, ~50ms creation)
  shim = tool interception (future — not in this module)
  session = tape (already exists in storage/fork_tape.py)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from krewcli.agents.code_refs import run_command

logger = logging.getLogger(__name__)

WORKTREE_ISOLATION_ENV = "KREWCLI_WORKTREE_ISOLATION"
_WORKTREE_BASE = "/tmp/krew-sandbox"


def is_worktree_isolation_enabled() -> bool:
    """Check if worktree isolation is enabled via env var."""
    raw = os.getenv(WORKTREE_ISOLATION_ENV, "").strip().lower()
    return raw in {"1", "true", "yes"}


@dataclass(frozen=True)
class CodeDiff:
    """Full diff captured from a task's worktree."""
    baseline_sha: str
    head_sha: str
    diff_text: str
    files_changed: dict[str, str]  # {path: "A"|"M"|"D"|"R"}
    stat: str
    commits: list[CommitInfo] = field(default_factory=list)


@dataclass(frozen=True)
class CommitInfo:
    """A single commit in the worktree."""
    sha: str
    message: str


class WorktreeManager:
    """Manages per-task git worktrees for sandbox isolation.

    Usage::

        mgr = WorktreeManager(repo_dir="/path/to/repo")
        wt_path = await mgr.create_worktree("abc123", "bun-1", "task-1")
        # ... agent executes in wt_path ...
        diff = await mgr.collect_diff(wt_path, "abc123")
        await mgr.cleanup_worktree(wt_path, "bun-1", "task-1")
    """

    def __init__(self, repo_dir: str) -> None:
        self._repo_dir = repo_dir

    async def create_worktree(
        self,
        baseline_sha: str,
        bundle_id: str,
        task_id: str,
    ) -> str:
        """Create an isolated git worktree for a task.

        Returns the worktree path.
        """
        branch_name = f"sandbox/{bundle_id}/{task_id}"
        wt_path = f"{_WORKTREE_BASE}/{task_id}"

        os.makedirs(_WORKTREE_BASE, exist_ok=True)

        result = await run_command(
            ["git", "worktree", "add", "-b", branch_name, wt_path, baseline_sha],
            self._repo_dir,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create worktree at {wt_path}: {result.stderr}"
            )

        logger.info(
            "worktree: created %s from %s for task %s",
            wt_path, baseline_sha[:8], task_id,
        )
        return wt_path

    async def collect_diff(
        self,
        worktree_path: str,
        baseline_sha: str,
    ) -> CodeDiff:
        """Collect the full diff from a worktree after task execution."""
        head_result = await run_command(
            ["git", "rev-parse", "HEAD"], worktree_path,
        )
        head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""

        # Compare the baseline commit to the worktree itself, not just HEAD.
        # Gateway agents usually leave edits uncommitted, so `baseline..HEAD`
        # would miss the actual task diff.
        diff_result = await run_command(
            ["git", "diff", baseline_sha], worktree_path, timeout=60,
        )
        diff_text = diff_result.stdout if diff_result.returncode == 0 else ""

        stat_result = await run_command(
            ["git", "diff", "--stat", baseline_sha], worktree_path,
        )
        stat = stat_result.stdout.strip() if stat_result.returncode == 0 else ""

        name_status_result = await run_command(
            ["git", "diff", "--name-status", baseline_sha], worktree_path,
        )
        files_changed = _parse_name_status(
            name_status_result.stdout if name_status_result.returncode == 0 else ""
        )

        log_result = await run_command(
            ["git", "log", "--oneline", f"{baseline_sha}..HEAD"], worktree_path,
        )
        commits = _parse_log(
            log_result.stdout if log_result.returncode == 0 else ""
        )

        return CodeDiff(
            baseline_sha=baseline_sha,
            head_sha=head_sha,
            diff_text=diff_text,
            files_changed=files_changed,
            stat=stat,
            commits=commits,
        )

    async def cleanup_worktree(
        self,
        worktree_path: str,
        bundle_id: str,
        task_id: str,
    ) -> None:
        """Remove a worktree and its branch after task completion."""
        branch_name = f"sandbox/{bundle_id}/{task_id}"

        result = await run_command(
            ["git", "worktree", "remove", "--force", worktree_path],
            self._repo_dir,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "worktree: failed to remove %s: %s",
                worktree_path, result.stderr,
            )

        branch_result = await run_command(
            ["git", "branch", "-D", branch_name],
            self._repo_dir,
            timeout=10,
        )
        if branch_result.returncode != 0:
            logger.debug(
                "worktree: branch cleanup failed for %s: %s",
                branch_name, branch_result.stderr,
            )

        logger.info("worktree: cleaned up %s", worktree_path)


def _parse_name_status(output: str) -> dict[str, str]:
    """Parse `git diff --name-status` output into {path: status}."""
    result: dict[str, str] = {}
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[-1] if status.startswith(("R", "C")) and len(parts) >= 3 else parts[1]
        result[path] = status[0]  # First char: A, M, D, R
    return result


def _parse_log(output: str) -> list[CommitInfo]:
    """Parse `git log --oneline` output into CommitInfo list."""
    commits: list[CommitInfo] = []
    for line in output.strip().splitlines():
        if not line:
            continue
        parts = line.split(" ", 1)
        sha = parts[0]
        message = parts[1] if len(parts) > 1 else ""
        commits.append(CommitInfo(sha=sha, message=message))
    return commits
