"""Local cookbook git operations — clone, submodule, push.

Wraps git subprocess calls for the onboard flow. The cookbook repo
is cloned from krewhub, recipes are added as submodules, and changes
are pushed back (triggering krewhub's post-receive indexing).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class CookbookRepoError(Exception):
    """Raised when a git operation fails."""


async def clone_or_fetch(clone_url: str, target_dir: str) -> bool:
    """Clone cookbook repo, or fetch if target_dir already exists.

    Returns True on success.
    """
    target = Path(target_dir)
    if target.exists() and (target / ".git").exists():
        logger.info("Cookbook repo already exists at %s, fetching", target_dir)
        rc, _, stderr = await _git(["fetch", "--all"], cwd=target_dir)
        if rc != 0:
            raise CookbookRepoError(f"git fetch failed: {stderr}")
        return True

    if target.exists() and (target / ".git").is_file():
        logger.info("Cookbook repo already exists at %s, fetching", target_dir)
        rc, _, stderr = await _git(["fetch", "--all"], cwd=target_dir)
        if rc != 0:
            raise CookbookRepoError(f"git fetch failed: {stderr}")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)

    rc, _, stderr = await _git(
        ["clone", clone_url, target_dir],
        cwd=str(target.parent),
    )
    if rc != 0:
        if "empty repository" in stderr.lower() or "warning" in stderr.lower():
            target.mkdir(parents=True, exist_ok=True)
            await _git(["init"], cwd=target_dir)
            await _git(["remote", "add", "origin", clone_url], cwd=target_dir)
            await _git(["checkout", "-b", "main"], cwd=target_dir)
            logger.info("Initialized empty cookbook repo at %s", target_dir)
            return True
        raise CookbookRepoError(f"git clone failed: {stderr}")

    return True


async def add_recipe_submodule(
    cookbook_dir: str,
    name: str,
    repo_url: str,
    branch: str = "main",
) -> bool:
    """Add a recipe as a git submodule. Returns True if added, False if already present."""
    safe_name = sanitize_name(name)
    submodule_path = os.path.join(cookbook_dir, safe_name)

    if os.path.exists(submodule_path) and os.listdir(submodule_path):
        logger.info("Submodule %s already exists, skipping", safe_name)
        return False

    gitmodules = os.path.join(cookbook_dir, ".gitmodules")
    if os.path.exists(gitmodules):
        content = Path(gitmodules).read_text()
        if f'path = {safe_name}' in content:
            logger.info("Submodule %s already in .gitmodules, skipping", safe_name)
            return False

    rc, _, stderr = await _git(
        ["submodule", "add", "-b", branch, repo_url, safe_name],
        cwd=cookbook_dir,
    )
    if rc != 0:
        raise CookbookRepoError(
            f"git submodule add failed for {name}: {stderr}"
        )

    return True


async def commit_and_push(cookbook_dir: str, message: str) -> bool:
    """Stage all, commit, push. Returns True if pushed, False if nothing to commit."""
    await _git(["add", "-A"], cwd=cookbook_dir)

    rc, stdout, _ = await _git(
        ["status", "--porcelain"], cwd=cookbook_dir,
    )
    if not stdout.strip():
        logger.info("Nothing to commit in %s", cookbook_dir)
        return False

    rc, _, stderr = await _git(
        ["commit", "-m", message], cwd=cookbook_dir,
    )
    if rc != 0:
        raise CookbookRepoError(f"git commit failed: {stderr}")

    rc, _, stderr = await _git(
        ["push", "origin", "HEAD"], cwd=cookbook_dir,
    )
    if rc != 0:
        raise CookbookRepoError(f"git push failed: {stderr}")

    return True


async def sync_submodules(cookbook_dir: str) -> bool:
    """Initialize and update submodules. Returns True on success."""
    rc, _, stderr = await _git(
        ["submodule", "update", "--init", "--recursive"],
        cwd=cookbook_dir,
    )
    if rc != 0:
        raise CookbookRepoError(f"git submodule update failed: {stderr}")
    return True


async def configure_git_user(
    cookbook_dir: str, name: str, email: str,
) -> None:
    """Set local git user.name and user.email for the cookbook repo."""
    await _git(["config", "user.name", name], cwd=cookbook_dir)
    await _git(["config", "user.email", email], cwd=cookbook_dir)


def sanitize_name(name: str) -> str:
    """Sanitize recipe name for filesystem use."""
    import re
    return re.sub(r"[^a-zA-Z0-9_\-.]", "-", name)


async def _git(
    args: list[str], cwd: str,
) -> tuple[int, str, str]:
    """Run a git command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
    )
