"""OpenCode hook config writer.

Drops `<workspace_dir>/.opencode/plugins/krewcli-bridge.js` (a
ported copy of vibe-island's claude-opencode-hook.js) and registers
it via `<workspace_dir>/.opencode/opencode.json`. The plugin runs
inside opencode's own runtime, listens to its internal event bus,
and POSTs canonical hook events to krewhub.

OpenCode is the one supported agent that doesn't fire OS-level
process hooks — it has a plugin model instead. Vibe-island handled
this exact case the same way; we ported their JS verbatim with the
unix-socket destination swapped for HTTP.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from pathlib import Path

from krewcli.hooks.types import HookWiring

logger = logging.getLogger(__name__)

PLUGIN_FILENAME = "krewcli-bridge.js"
OPENCODE_CONFIG_FILENAME = "opencode.json"


def _read_plugin_source() -> str:
    """Read the embedded JS plugin source from the package."""
    with resources.files("krewcli.hooks.adapters").joinpath(
        "opencode_plugin.js"
    ).open("r", encoding="utf-8") as f:
        return f.read()


def write(workspace_dir: str) -> HookWiring:
    opencode_dir = Path(workspace_dir) / ".opencode"
    plugins_dir = opencode_dir / "plugins"
    plugin_path = plugins_dir / PLUGIN_FILENAME
    config_path = opencode_dir / OPENCODE_CONFIG_FILENAME

    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("opencode writer: failed to create %s: %s", plugins_dir, exc)
        return HookWiring(source="opencode", notes=f"mkdir failed: {exc}")

    files_written: list[Path] = []

    try:
        plugin_path.write_text(_read_plugin_source())
        files_written.append(plugin_path.resolve())
    except (OSError, FileNotFoundError) as exc:
        logger.warning("opencode writer: plugin write failed: %s", exc)
        return HookWiring(source="opencode", notes=f"plugin write failed: {exc}")

    # Register the plugin in opencode.json. Merge with any existing
    # config the user has so we don't clobber settings.
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}

    plugins = existing.get("plugins", []) or []
    plugin_ref = f"./plugins/{PLUGIN_FILENAME}"
    if plugin_ref not in plugins:
        plugins.append(plugin_ref)
    existing["plugins"] = plugins

    try:
        config_path.write_text(json.dumps(existing, indent=2) + "\n")
        files_written.append(config_path.resolve())
    except OSError as exc:
        logger.warning("opencode writer: config write failed: %s", exc)
        return HookWiring(source="opencode", notes=f"config write failed: {exc}")

    return HookWiring(
        source="opencode",
        settings_file=config_path.resolve(),
        # OpenCode reads OPENCODE_CONFIG / OPENCODE_HOME for config
        # discovery; the SpawnManager codex agent runner can set those
        # if/when we wire opencode in.
        env={
            "KREWCLI_OPENCODE_PLUGIN_FILE": str(plugin_path.resolve()),
            "OPENCODE_CONFIG": str(config_path.resolve()),
        },
        files_written=files_written,
        notes="JS plugin ported from vibe-island; runs inside opencode runtime",
    )
