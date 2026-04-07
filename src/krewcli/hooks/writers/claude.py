"""Claude Code hook config writer.

Writes `<workspace_dir>/.claude/krewcli-hooks.json` containing
PreToolUse / PostToolUse / Stop / SessionStart hook entries that
invoke `krewcli bridge --source claude <event>`. Loaded explicitly
by the agent runner via Claude's `--settings <path>` flag, which
bypasses the project trust prompt and the hook approval flow.

NO global filesystem mutation. Per-spawn `--settings` is the sole
mechanism we use.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from krewcli.hooks.types import HookWiring

logger = logging.getLogger(__name__)

_HOOK_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SessionStart")
SETTINGS_FILENAME = "krewcli-hooks.json"


def write(workspace_dir: str) -> HookWiring:
    krewcli_bin = shutil.which("krewcli") or "krewcli"
    settings_dir = Path(workspace_dir) / ".claude"
    settings_path = settings_dir / SETTINGS_FILENAME

    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("claude writer: failed to create %s: %s", settings_dir, exc)
        return HookWiring(source="claude", notes=f"mkdir failed: {exc}")

    hook_config: dict[str, list[dict]] = {}
    for event_name in _HOOK_EVENTS:
        cmd = f"{krewcli_bin} bridge --source claude {event_name}"
        hook_config[event_name] = [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": cmd}],
            }
        ]

    payload = {"hooks": hook_config}
    try:
        settings_path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        logger.warning("claude writer: failed to write %s: %s", settings_path, exc)
        return HookWiring(source="claude", notes=f"write failed: {exc}")

    abs_path = settings_path.resolve()
    return HookWiring(
        source="claude",
        settings_file=abs_path,
        extra_args=["--settings", str(abs_path)],
        env={"KREWCLI_CLAUDE_SETTINGS_FILE": str(abs_path)},
        files_written=[abs_path],
        notes="loaded via --settings; no global filesystem mutation",
    )
