"""Per-source hook normalizers.

Each agent's hook system has a different stdin payload shape. The
normalizer for that source maps it to the canonical
`CanonicalHookEvent`. The bridge CLI looks the right normalizer up
by `--source <name>`.

To add a new agent: drop a `<name>.py` here exporting `normalize`
and register it in `_REGISTRY` below.
"""

from __future__ import annotations

from typing import Callable

from krewcli.bridge.canonical import CanonicalHookEvent
from krewcli.bridge.sources import (
    claude as _claude,
    codex as _codex,
    cursor as _cursor,
    droid as _droid,
    gemini as _gemini,
    opencode as _opencode,
)

Normalizer = Callable[[str, dict], CanonicalHookEvent]

_REGISTRY: dict[str, Normalizer] = {
    "claude": _claude.normalize,
    "codex": _codex.normalize,
    "cursor": _cursor.normalize,
    "droid": _droid.normalize,
    "gemini": _gemini.normalize,
    "opencode": _opencode.normalize,
}


def get_normalizer(source: str) -> Normalizer:
    """Return the normalizer for an agent name.

    Falls back to the claude normalizer because Claude's payload
    shape is the de-facto canonical one and many agents emit
    something close to it.
    """
    return _REGISTRY.get(source, _claude.normalize)


def supported_sources() -> list[str]:
    return sorted(_REGISTRY.keys())
