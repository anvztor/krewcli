from __future__ import annotations

import pytest

from krewcli.agents.code_refs import CommandResult
from krewcli.gateway.worktree import WorktreeManager, _parse_name_status


def test_parse_name_status_uses_destination_path_for_renames():
    parsed = _parse_name_status(
        "M\tsrc/app.py\nR100\told.py\tnew.py\nA\tnotes.txt\nD\tgone.txt\n"
    )

    assert parsed == {
        "src/app.py": "M",
        "new.py": "R",
        "notes.txt": "A",
        "gone.txt": "D",
    }


@pytest.mark.asyncio
async def test_collect_diff_includes_uncommitted_worktree_changes(monkeypatch):
    calls: list[tuple[list[str], str, int]] = []

    async def fake_run_command(args, working_dir, *, timeout=30):
        calls.append((args, working_dir, timeout))
        if args == ["git", "rev-parse", "HEAD"]:
            return CommandResult(0, "head123\n", "")
        if args == ["git", "diff", "base123"]:
            return CommandResult(0, "diff --git a/file.py b/file.py\n", "")
        if args == ["git", "diff", "--stat", "base123"]:
            return CommandResult(0, " file.py | 1 +\n", "")
        if args == ["git", "diff", "--name-status", "base123"]:
            return CommandResult(0, "M\tfile.py\n", "")
        if args == ["git", "log", "--oneline", "base123..HEAD"]:
            return CommandResult(0, "head123 update file\n", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("krewcli.gateway.worktree.run_command", fake_run_command)

    diff = await WorktreeManager("/repo").collect_diff("/repo/.wt/task-1", "base123")

    assert diff.head_sha == "head123"
    assert diff.diff_text == "diff --git a/file.py b/file.py\n"
    assert diff.files_changed == {"file.py": "M"}
    assert diff.stat == "file.py | 1 +"
    assert [commit.sha for commit in diff.commits] == ["head123"]
    assert [args for args, _working_dir, _timeout in calls] == [
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "base123"],
        ["git", "diff", "--stat", "base123"],
        ["git", "diff", "--name-status", "base123"],
        ["git", "log", "--oneline", "base123..HEAD"],
    ]
