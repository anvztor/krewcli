"""Gemini CLI hook config writer.

Writes `<workspace_dir>/.gemini/settings.json` declaring hook
entries that invoke `krewcli bridge --source gemini <event>`.

Vibe-island ships a Python adapter (`vibe-island-gemini-hook.py`)
because Gemini's hook payload doesn't match canonical out of the
box. We delegate normalization to the bridge's gemini source
normalizer instead of shipping a separate adapter.

Workspace-local; no global ~/.gemini mutation.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from krewcli.hooks.types import HookWiring

logger = logging.getLogger(__name__)

# Gemini's hook event names are still in flux upstream. We use the
# canonical Claude names; the bridge normalizer maps anything else.
_GEMINI_HOOK_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SessionStart")


def write(workspace_dir: str) -> HookWiring:
    krewcli_bin = shutil.which("krewcli") or "krewcli"
    gemini_dir = Path(workspace_dir) / ".gemini"
    settings_path = gemini_dir / "settings.json"

    try:
        gemini_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("gemini writer: failed to create %s: %s", gemini_dir, exc)
        return HookWiring(source="gemini", notes=f"mkdir failed: {exc}")

    hooks: dict[str, list[dict]] = {}
    for event in _GEMINI_HOOK_EVENTS:
        hooks[event] = [
            {
                "command": f"{krewcli_bin} bridge --source gemini {event}",
            }
        ]

    try:
        settings_path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n")
    except OSError as exc:
        logger.warning("gemini writer: write failed: %s", exc)
        return HookWiring(source="gemini", notes=f"write failed: {exc}")

    abs_path = settings_path.resolve()
    return HookWiring(
        source="gemini",
        settings_file=abs_path,
        env={"KREWCLI_GEMINI_SETTINGS_FILE": str(abs_path)},
        files_written=[abs_path],
        notes="hook contract still in flux upstream",
    )
