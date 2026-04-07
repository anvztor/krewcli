"""Cursor agent hook config writer.

Writes `<workspace_dir>/.cursor/hooks.json` declaring entries for
Cursor's native hook event names:

  beforeShellExecution, afterShellExecution
  beforeMCPExecution, afterMCPExecution
  afterAgentResponse, beforeSubmitPrompt
  afterAgentThought, postToolUseFailure

Cursor is IDE-coupled and we don't typically spawn it via
SpawnManager. This writer exists so users can ingest events from
a manually-installed Cursor hook config the same way other agents
ingest theirs.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from krewcli.hooks.types import HookWiring

logger = logging.getLogger(__name__)

_CURSOR_HOOK_EVENTS = (
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "afterAgentResponse",
    "beforeSubmitPrompt",
    "afterAgentThought",
    "postToolUseFailure",
)


def write(workspace_dir: str) -> HookWiring:
    krewcli_bin = shutil.which("krewcli") or "krewcli"
    cursor_dir = Path(workspace_dir) / ".cursor"
    hooks_path = cursor_dir / "hooks.json"

    try:
        cursor_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cursor writer: failed to create %s: %s", cursor_dir, exc)
        return HookWiring(source="cursor", notes=f"mkdir failed: {exc}")

    hooks: dict[str, list[dict]] = {}
    for event in _CURSOR_HOOK_EVENTS:
        hooks[event] = [
            {
                "command": f"{krewcli_bin} bridge --source cursor {event}",
            }
        ]

    try:
        hooks_path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n")
    except OSError as exc:
        logger.warning("cursor writer: write failed: %s", exc)
        return HookWiring(source="cursor", notes=f"write failed: {exc}")

    abs_path = hooks_path.resolve()
    return HookWiring(
        source="cursor",
        settings_file=abs_path,
        env={"KREWCLI_CURSOR_HOOKS_FILE": str(abs_path)},
        files_written=[abs_path],
        notes="cursor IDE picks this up via its hooks.json discovery",
    )
