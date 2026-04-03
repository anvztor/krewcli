"""Write hook configs into agent CLI config files.

Injects PostToolUse/Stop hooks pointing at the KrewCLI hook listener.
Backs up originals before modification.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CODEX_HOOKS_PATH = Path.home() / ".codex" / "hooks.json"


def configure_claude_hooks(listener_url: str) -> bool:
    """Inject hook entries into ~/.claude/settings.json."""
    return _inject_hooks(
        config_path=CLAUDE_SETTINGS_PATH,
        listener_url=listener_url,
        hook_format="claude",
    )


def configure_codex_hooks(listener_url: str) -> bool:
    """Inject hook entries into ~/.codex/hooks.json."""
    return _inject_hooks(
        config_path=CODEX_HOOKS_PATH,
        listener_url=listener_url,
        hook_format="codex",
    )


def remove_claude_hooks() -> bool:
    """Remove KrewCLI hooks from Claude settings, restore backup if available."""
    return _remove_hooks(CLAUDE_SETTINGS_PATH)


def remove_codex_hooks() -> bool:
    """Remove KrewCLI hooks from Codex settings, restore backup if available."""
    return _remove_hooks(CODEX_HOOKS_PATH)


def _inject_hooks(config_path: Path, listener_url: str, hook_format: str) -> bool:
    """Inject hooks into a JSON config file."""
    if not config_path.parent.exists():
        logger.info("Config dir %s does not exist, skipping", config_path.parent)
        return False

    # Read existing config
    config: dict = {}
    if config_path.exists():
        backup_path = config_path.with_suffix(f"{config_path.suffix}.krewcli-backup")
        shutil.copy2(config_path, backup_path)
        logger.info("Backed up %s to %s", config_path, backup_path)
        config = json.loads(config_path.read_text())

    # Build hook commands
    hooks = _build_hook_commands(listener_url, hook_format)

    # Merge into existing hooks (don't overwrite user's hooks)
    existing_hooks = config.get("hooks", {})
    for event_name, hook_entries in hooks.items():
        existing = existing_hooks.get(event_name, [])
        # Remove any previous krewcli hooks
        existing = [h for h in existing if not _is_krewcli_hook(h)]
        existing.extend(hook_entries)
        existing_hooks[event_name] = existing

    config["hooks"] = existing_hooks
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    logger.info("Injected hooks into %s", config_path)
    return True


def _remove_hooks(config_path: Path) -> bool:
    """Remove KrewCLI hooks from a config file."""
    if not config_path.exists():
        return False

    config = json.loads(config_path.read_text())
    hooks = config.get("hooks", {})
    changed = False

    for event_name in list(hooks.keys()):
        original = hooks[event_name]
        filtered = [h for h in original if not _is_krewcli_hook(h)]
        if len(filtered) != len(original):
            changed = True
        if filtered:
            hooks[event_name] = filtered
        else:
            del hooks[event_name]

    if changed:
        config["hooks"] = hooks
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        logger.info("Removed krewcli hooks from %s", config_path)

    return changed


def _build_hook_commands(listener_url: str, hook_format: str) -> dict[str, list[dict]]:
    """Build hook config entries for the given format."""
    events = ["PostToolUse", "Stop"]
    if hook_format == "claude":
        events.append("PreToolUse")

    hooks: dict[str, list[dict]] = {}
    for event in events:
        endpoint = f"{listener_url}/hooks/{event.lower()}"
        hooks[event] = [{
            "hooks": [{
                "type": "command",
                "command": f'curl -s -X POST {endpoint} -H "Content-Type: application/json" -d @- 2>/dev/null || true',
            }],
            "matcher": "*",
            "_krewcli": True,
        }]

    return hooks


def _is_krewcli_hook(hook: dict) -> bool:
    """Check if a hook entry was injected by KrewCLI."""
    if hook.get("_krewcli"):
        return True
    # Check flat format (legacy)
    command = hook.get("command", "")
    if "/hooks/" in command and "krewcli" in command.lower():
        return True
    # Check nested format
    inner_hooks = hook.get("hooks", [])
    for inner in inner_hooks:
        cmd = inner.get("command", "")
        if "/hooks/" in cmd and ("krewcli" in cmd.lower() or "9998" in cmd):
            return True
    return False
