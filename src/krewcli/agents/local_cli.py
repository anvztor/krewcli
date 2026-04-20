"""Local CLI agent boundary.

This module provides the refactored import surface for local CLI agent
helpers while keeping the implementation in ``krewcli.agents.base`` for
backward compatibility.
"""

from krewcli.agents.base import (
    CommandResult,
    LocalCliAgent,
    _DEFAULT_LOCAL_TIMEOUT,
    _MAX_LINE_CHARS,
    _STREAM_LIMIT,
    _drain_stream,
    _list_changed_files,
    _read_git_value,
    _run_command,
    _summarize_output,
)

__all__ = [
    "CommandResult",
    "LocalCliAgent",
    "_DEFAULT_LOCAL_TIMEOUT",
    "_MAX_LINE_CHARS",
    "_STREAM_LIMIT",
    "_drain_stream",
    "_list_changed_files",
    "_read_git_value",
    "_run_command",
    "_summarize_output",
]
