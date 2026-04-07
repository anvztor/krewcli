"""Per-spawn scoped hook config writer.

We do NOT touch ~/.claude/settings.json. Instead, before each spawn we
write a per-spawn settings file into a directory we own (the per-recipe
working dir, under `.claude/krewcli-hooks.json`) and the agent runner
loads it via Claude's `--settings <file>` flag. This bypasses the
project-local trust prompt and the hook approval flow because the
spawner is explicitly opting in to its own config.

Also constructs the env vars (KREWHUB_TASK_ID, etc) that the
`krewcli hook ingest` shim reads when it forwards events to krewhub.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Hooks we want to capture for the audit trail.
_HOOK_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SessionStart")

# Filename inside `<workdir>/.claude/` we own. Loaded explicitly via
# `claude --settings <path>`, NOT via auto-discovery.
SCOPED_HOOKS_FILENAME = "krewcli-hooks.json"


def write_claude_scoped_hooks(workdir: str) -> Path | None:
    """Write the per-spawn hooks file under `<workdir>/.claude/`.

    The hooks invoke the `krewcli hook ingest <event-name>` shim. The
    shim reads its config from KREWHUB_* env vars set by SpawnManager.
    Returns the absolute path that was written, or None on failure.
    """
    krewcli_bin = shutil.which("krewcli") or "krewcli"
    settings_dir = Path(workdir) / ".claude"
    settings_path = settings_dir / SCOPED_HOOKS_FILENAME

    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create %s: %s", settings_dir, exc)
        return None

    hook_config: dict[str, list[dict]] = {}
    for event_name in _HOOK_EVENTS:
        cmd = f'{krewcli_bin} hook ingest {event_name}'
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
        logger.warning("Failed to write %s: %s", settings_path, exc)
        return None

    return settings_path.resolve()


def build_hook_env(
    *,
    task_id: str,
    bundle_id: str,
    recipe_id: str,
    agent_id: str,
    krewhub_url: str,
    api_key: str,
) -> dict[str, str]:
    """Build the env vars consumed by `krewcli hook ingest`."""
    return {
        "KREWHUB_TASK_ID": task_id or "",
        "KREWHUB_BUNDLE_ID": bundle_id or "",
        "KREWHUB_RECIPE_ID": recipe_id or "",
        "KREWHUB_AGENT_ID": agent_id or "spawned-agent",
        "KREWHUB_URL": krewhub_url or "",
        "KREWHUB_API_KEY": api_key or "",
    }


