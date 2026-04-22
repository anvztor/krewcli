"""Unified agent backend protocol and implementations.

The backend package provides a streaming-first interface for executing
AI coding agents. Each backend wraps a local CLI tool (Claude Code,
Codex, Bub) behind a common ``Backend`` protocol that produces a
``BackendSession`` — an async queue of ``BackendMessage`` events plus
a terminal ``BackendResult``.

This decouples agent execution from event streaming (krewhub),
task management (daemon), and CLI wiring. The backend knows nothing
about tasks, sessions, or krewhub — it just runs a prompt and
streams what happens.
"""

from krewcli.backend.protocol import (
    Backend,
    BackendMessage,
    BackendResult,
    BackendSession,
)
from krewcli.backend.registry import available_backends, get_backend

__all__ = [
    "Backend",
    "BackendMessage",
    "BackendResult",
    "BackendSession",
    "available_backends",
    "get_backend",
]
