"""Tests for cookbook_repo git operations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from krewcli.cookbook_repo import (
    CookbookRepoError,
    add_recipe_submodule,
    clone_or_fetch,
    commit_and_push,
    configure_git_user,
    sync_submodules,
    sanitize_name,
)


@pytest.fixture(autouse=True)
def _allow_file_protocol_env(monkeypatch):
    """Allow git file:// protocol for all tests in this module."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _init_bare_repo(path: str) -> None:
    """Create a bare git repo with an initial commit on 'main'."""
    bare = Path(path)
    bare.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "init", "--bare", str(bare),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Need an initial commit so clone works (non-empty repo)
    staging = Path(str(bare) + "_staging")
    staging.mkdir(parents=True, exist_ok=True)

    for cmd in [
        ["git", "clone", str(bare), str(staging)],
        ["git", "-C", str(staging), "checkout", "-b", "main"],
        ["git", "-C", str(staging), "config", "user.name", "test"],
        ["git", "-C", str(staging), "config", "user.email", "test@test"],
    ]:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await p.communicate()

    # Create an initial file and commit
    (staging / "README.md").write_text("# Test\n")
    for cmd in [
        ["git", "-C", str(staging), "add", "-A"],
        ["git", "-C", str(staging), "commit", "-m", "initial"],
        ["git", "-C", str(staging), "push", "origin", "main"],
    ]:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await p.communicate()

    import shutil
    shutil.rmtree(staging, ignore_errors=True)


async def _init_recipe_bare(path: str) -> None:
    """Create a bare repo suitable as a recipe submodule source."""
    await _init_bare_repo(path)


# ---------------------------------------------------------------------------
# _sanitize_name
# ---------------------------------------------------------------------------

def test_sanitize_name_basic():
    assert sanitize_name("my-recipe") == "my-recipe"


def test_sanitize_name_special_chars():
    result = sanitize_name("my recipe!@#$%")
    # Each non-alphanumeric/non-dot/non-underscore/non-dash char replaced with -
    assert result.startswith("my-recipe")
    assert all(c in "abcdefghijklmnopqrstuvwxyz-" for c in result)


def test_sanitize_name_dots_underscores():
    assert sanitize_name("recipe_v1.2") == "recipe_v1.2"


# ---------------------------------------------------------------------------
# clone_or_fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clone_or_fetch_clones(tmp_path):
    bare = str(tmp_path / "bare.git")
    await _init_bare_repo(bare)

    target = str(tmp_path / "cloned")
    result = await clone_or_fetch(bare, target)
    assert result is True
    assert (Path(target) / ".git").exists()
    assert (Path(target) / "README.md").exists()


@pytest.mark.asyncio
async def test_clone_or_fetch_fetches_existing(tmp_path):
    bare = str(tmp_path / "bare.git")
    await _init_bare_repo(bare)

    target = str(tmp_path / "cloned")
    await clone_or_fetch(bare, target)

    # Second call should fetch, not fail
    result = await clone_or_fetch(bare, target)
    assert result is True


# ---------------------------------------------------------------------------
# add_recipe_submodule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_recipe_submodule(tmp_path):
    cookbook_bare = str(tmp_path / "cookbook.git")
    await _init_bare_repo(cookbook_bare)

    recipe_bare = str(tmp_path / "recipe.git")
    await _init_recipe_bare(recipe_bare)

    cookbook_dir = str(tmp_path / "cookbook")
    await clone_or_fetch(cookbook_bare, cookbook_dir)
    await configure_git_user(cookbook_dir, "test", "test@test")

    added = await add_recipe_submodule(
        cookbook_dir, "my-recipe", recipe_bare, branch="main",
    )
    assert added is True
    assert (Path(cookbook_dir) / "my-recipe").exists()


@pytest.mark.asyncio
async def test_add_recipe_submodule_skips_duplicate(tmp_path):
    cookbook_bare = str(tmp_path / "cookbook.git")
    await _init_bare_repo(cookbook_bare)

    recipe_bare = str(tmp_path / "recipe.git")
    await _init_recipe_bare(recipe_bare)

    cookbook_dir = str(tmp_path / "cookbook")
    await clone_or_fetch(cookbook_bare, cookbook_dir)
    await configure_git_user(cookbook_dir, "test", "test@test")

    await add_recipe_submodule(cookbook_dir, "my-recipe", recipe_bare)

    # Second add should skip
    added = await add_recipe_submodule(cookbook_dir, "my-recipe", recipe_bare)
    assert added is False


# ---------------------------------------------------------------------------
# commit_and_push
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_and_push(tmp_path):
    bare = str(tmp_path / "bare.git")
    await _init_bare_repo(bare)

    clone_dir = str(tmp_path / "clone")
    await clone_or_fetch(bare, clone_dir)
    await configure_git_user(clone_dir, "test", "test@test")

    # Add a file
    (Path(clone_dir) / "new.txt").write_text("hello\n")

    pushed = await commit_and_push(clone_dir, "add new.txt")
    assert pushed is True


@pytest.mark.asyncio
async def test_commit_and_push_noop_when_clean(tmp_path):
    bare = str(tmp_path / "bare.git")
    await _init_bare_repo(bare)

    clone_dir = str(tmp_path / "clone")
    await clone_or_fetch(bare, clone_dir)
    await configure_git_user(clone_dir, "test", "test@test")

    # Nothing to commit
    pushed = await commit_and_push(clone_dir, "nothing")
    assert pushed is False


# ---------------------------------------------------------------------------
# sync_submodules
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_submodules(tmp_path):
    bare = str(tmp_path / "bare.git")
    await _init_bare_repo(bare)

    clone_dir = str(tmp_path / "clone")
    await clone_or_fetch(bare, clone_dir)

    # Should succeed even with no submodules
    result = await sync_submodules(clone_dir)
    assert result is True
