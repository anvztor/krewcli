"""Daemon module — pull-based task execution loop.

Replaces the old gateway/lifecycle.py + a2a/ framework with a simple
daemon that polls krewhub for tasks, claims them, executes via the
Backend protocol, and streams events through the Session abstraction.

Architecture follows Anthropic's Managed Agents pattern:
  - Brain: Backend (LLM + CLI harness)
  - Hands: ExecutionEnvironment (isolated workdir)
  - Session: append-only event log persisted in krewhub
"""

from krewcli.daemon.loop import DaemonLoop

__all__ = ["DaemonLoop"]
