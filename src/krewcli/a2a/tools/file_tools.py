"""File operation tools for framework agents."""

from __future__ import annotations

import os

from pydantic_ai import RunContext
from krewcli.a2a.tools.bash_tool import TaskDeps


async def read_file(ctx: RunContext[TaskDeps], path: str) -> str:
    """Read a file's contents.

    Args:
        path: File path relative to working directory (or absolute).

    Returns:
        File contents with line numbers, or error message.
    """
    full_path = _resolve(ctx.deps.working_dir, path)
    try:
        with open(full_path, "r") as f:
            lines = f.readlines()
        numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines[:500])]
        result = "".join(numbered)
        if len(lines) > 500:
            result += f"\n... ({len(lines) - 500} more lines)"
        return result
    except Exception as exc:
        return f"Error reading {path}: {exc}"


async def write_file(ctx: RunContext[TaskDeps], path: str, content: str) -> str:
    """Write content to a file (creates or overwrites).

    Args:
        path: File path relative to working directory.
        content: The full file content to write.

    Returns:
        Confirmation message.
    """
    full_path = _resolve(ctx.deps.working_dir, path)
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error writing {path}: {exc}"


async def edit_file(ctx: RunContext[TaskDeps], path: str, old_string: str, new_string: str) -> str:
    """Replace a string in a file (exact match, first occurrence).

    Args:
        path: File path relative to working directory.
        old_string: The exact text to find.
        new_string: The replacement text.

    Returns:
        Confirmation or error message.
    """
    full_path = _resolve(ctx.deps.working_dir, path)
    try:
        with open(full_path, "r") as f:
            content = f.read()

        if old_string not in content:
            return f"Error: old_string not found in {path}"

        updated = content.replace(old_string, new_string, 1)
        with open(full_path, "w") as f:
            f.write(updated)
        return f"Replaced in {path}"
    except Exception as exc:
        return f"Error editing {path}: {exc}"


def _resolve(working_dir: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(working_dir, path)
