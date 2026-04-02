"""Tests for agent tools (bash, file, git)."""

from __future__ import annotations

import os
import tempfile

import pytest

from krewcli.a2a.tools.bash_tool import TaskDeps


@pytest.fixture
def tmp_workdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def deps(tmp_workdir):
    return TaskDeps(working_dir=tmp_workdir)


class FakeRunContext:
    """Minimal RunContext mock for tool testing."""
    def __init__(self, deps):
        self.deps = deps


@pytest.mark.asyncio
async def test_bash_exec(deps, tmp_workdir):
    from krewcli.a2a.tools.bash_tool import bash_exec
    ctx = FakeRunContext(deps)
    result = await bash_exec(ctx, "echo hello")
    assert "hello" in result


@pytest.mark.asyncio
async def test_bash_exec_timeout(deps):
    from krewcli.a2a.tools.bash_tool import bash_exec
    ctx = FakeRunContext(deps)
    # This shouldn't hang — timeout is 120s, but we test a fast command
    result = await bash_exec(ctx, "echo fast")
    assert "fast" in result


@pytest.mark.asyncio
async def test_read_file(deps, tmp_workdir):
    from krewcli.a2a.tools.file_tools import read_file
    path = os.path.join(tmp_workdir, "test.txt")
    with open(path, "w") as f:
        f.write("line one\nline two\n")

    ctx = FakeRunContext(deps)
    result = await read_file(ctx, "test.txt")
    assert "line one" in result
    assert "line two" in result
    # Should have line numbers
    assert "1 |" in result or "   1 |" in result


@pytest.mark.asyncio
async def test_read_file_not_found(deps):
    from krewcli.a2a.tools.file_tools import read_file
    ctx = FakeRunContext(deps)
    result = await read_file(ctx, "nonexistent.txt")
    assert "Error" in result


@pytest.mark.asyncio
async def test_write_file(deps, tmp_workdir):
    from krewcli.a2a.tools.file_tools import write_file
    ctx = FakeRunContext(deps)
    result = await write_file(ctx, "output.txt", "hello world")
    assert "Wrote" in result
    assert os.path.exists(os.path.join(tmp_workdir, "output.txt"))
    with open(os.path.join(tmp_workdir, "output.txt")) as f:
        assert f.read() == "hello world"


@pytest.mark.asyncio
async def test_write_file_creates_dirs(deps, tmp_workdir):
    from krewcli.a2a.tools.file_tools import write_file
    ctx = FakeRunContext(deps)
    result = await write_file(ctx, "sub/dir/file.txt", "nested")
    assert "Wrote" in result
    assert os.path.exists(os.path.join(tmp_workdir, "sub", "dir", "file.txt"))


@pytest.mark.asyncio
async def test_edit_file(deps, tmp_workdir):
    from krewcli.a2a.tools.file_tools import edit_file
    path = os.path.join(tmp_workdir, "edit_me.txt")
    with open(path, "w") as f:
        f.write("foo bar baz")

    ctx = FakeRunContext(deps)
    result = await edit_file(ctx, "edit_me.txt", "bar", "qux")
    assert "Replaced" in result
    with open(path) as f:
        assert f.read() == "foo qux baz"


@pytest.mark.asyncio
async def test_edit_file_not_found(deps):
    from krewcli.a2a.tools.file_tools import edit_file
    ctx = FakeRunContext(deps)
    result = await edit_file(ctx, "missing.txt", "a", "b")
    assert "Error" in result


@pytest.mark.asyncio
async def test_edit_file_string_not_found(deps, tmp_workdir):
    from krewcli.a2a.tools.file_tools import edit_file
    path = os.path.join(tmp_workdir, "no_match.txt")
    with open(path, "w") as f:
        f.write("hello")

    ctx = FakeRunContext(deps)
    result = await edit_file(ctx, "no_match.txt", "xyz", "abc")
    assert "not found" in result


@pytest.mark.asyncio
async def test_git_status(tmp_workdir):
    from krewcli.a2a.tools.git_tools import git_status
    # Init a git repo
    os.system(f"cd {tmp_workdir} && git init -q && git config user.email test@test && git config user.name test")

    deps = TaskDeps(working_dir=tmp_workdir)
    ctx = FakeRunContext(deps)
    result = await git_status(ctx)
    # Empty repo, no files — should be clean or show nothing
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_git_diff(tmp_workdir):
    from krewcli.a2a.tools.git_tools import git_diff
    os.system(f"cd {tmp_workdir} && git init -q && git config user.email test@test && git config user.name test")

    deps = TaskDeps(working_dir=tmp_workdir)
    ctx = FakeRunContext(deps)
    result = await git_diff(ctx)
    assert isinstance(result, str)
