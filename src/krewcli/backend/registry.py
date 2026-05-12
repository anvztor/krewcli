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

    Drops backends whose CLI isn't installed on PATH with a loud warning
    (registration would otherwise advertise the agent as online, krewhub
    would dispatch tasks to it, and every task would immediately fail
    with "Codex CLI not found on PATH" — burning task generations).
    Set ``KREWCLI_STRICT_BACKENDS=1`` to fail-fast instead of dropping.
    """
    import logging
    log = logging.getLogger(__name__)
    _ensure_registered()

    strict = os.getenv("KREWCLI_STRICT_BACKENDS", "").strip().lower() in {"1", "true", "yes"}

    if requested is not None:
        backends: dict[str, Backend] = {}
        for name in requested:
            backend = get_backend(name)
            # is_available is duck-typed on the Backend protocol; backends
            # without it are assumed available (e.g. echo).
            if getattr(backend, "is_available", lambda: True)():
                backends[name] = backend
                continue
            msg = (
                f"backend {name!r} CLI not found on PATH "
                f"(PATH={os.environ.get('PATH', '')[:200]})"
            )
            if strict:
                raise RuntimeError(msg)
            log.warning(
                "%s — DROPPING from registration so krewhub won't "
                "dispatch tasks to a non-functional agent. Install the "
                "CLI or remove it from --agents to silence this.",
                msg,
            )
        return backends

    # Auto-detect: check which CLIs are available.
    backends = {}
    for name in _BACKEND_FACTORIES:
        if name == "echo":
            if os.getenv("KREWCLI_BACKEND_ECHO", "").strip().lower() in {"1", "true", "yes"}:
                backends["echo"] = get_backend("echo")
            continue
        backend = get_backend(name)
        if not getattr(backend, "is_available", lambda: True)():
            continue
        backends[name] = backend
    return backends
