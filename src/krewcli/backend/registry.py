"""Backend registry — discover and instantiate agent backends."""

from __future__ import annotations

import os
from typing import Any, Callable

from krewcli.backend.protocol import Backend

# Lazy imports to avoid pulling in all backends at module load.
_BACKEND_FACTORIES: dict[str, Callable[[], Backend]] = {}


def _ensure_registered() -> None:
    """Register all known backends on first access."""
    if _BACKEND_FACTORIES:
        return

    from krewcli.backend.claude import ClaudeBackend
    from krewcli.backend.codex import CodexBackend
    from krewcli.backend.gemini import GeminiBackend
    from krewcli.backend.bub import BubBackend
    from krewcli.backend.echo import EchoBackend

    _BACKEND_FACTORIES.update({
        "claude": ClaudeBackend,
        "codex": CodexBackend,
        "gemini": GeminiBackend,
        "bub": BubBackend,
        "echo": EchoBackend,
    })


# Agent capabilities advertised to krewhub during registration.
BACKEND_INFO: dict[str, dict[str, Any]] = {
    "claude": {
        "display_name": "Claude",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
    "codex": {
        "display_name": "Codex",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
    "gemini": {
        "display_name": "Gemini",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
    "bub": {
        "display_name": "Bub",
        "capabilities": ["claim", "milestones", "facts", "code_refs", "generate-graph"],
    },
    "echo": {
        "display_name": "Echo (test)",
        "capabilities": ["claim", "milestones"],
    },
}


def get_backend(name: str) -> Backend:
    """Instantiate a backend by name.

    Raises ValueError if the name is unknown.
    """
    _ensure_registered()
    factory = _BACKEND_FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown backend: {name}. "
            f"Available: {list(_BACKEND_FACTORIES.keys())}"
        )
    return factory()


def available_backends() -> list[str]:
    """Return names of all registered backends."""
    _ensure_registered()
    return list(_BACKEND_FACTORIES.keys())


def resolve_backends(requested: list[str] | None = None) -> dict[str, Backend]:
    """Resolve requested backend names to instances.

    If ``requested`` is None, auto-detect available CLIs on PATH.
    The echo backend is included when ``KREWCLI_BACKEND_ECHO=1``.
    """
    _ensure_registered()

    if requested is not None:
        return {name: get_backend(name) for name in requested}

    # Auto-detect: check which CLIs are available.
    backends: dict[str, Backend] = {}
    for name in _BACKEND_FACTORIES:
        if name == "echo":
            if os.getenv("KREWCLI_BACKEND_ECHO", "").strip().lower() in {"1", "true", "yes"}:
                backends["echo"] = get_backend("echo")
            continue
        backend = get_backend(name)
        # health() is async but we're in sync context — just include
        # all non-echo backends; health check happens at daemon start.
        backends[name] = backend
    return backends
