"""Spawn agent CLI processes in tmux sessions.

Each agent gets its own tmux session with a greeting prompt that
wakes it up and makes it ready to receive work.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

logger = logging.getLogger(__name__)

TMUX_SESSION_PREFIX = "krew"

GREETING_PROMPTS = {
    "claude": (
        "You are an agent in a Cookrew cookbook. "
        "You will receive tasks via this session. "
        "Confirm you are ready by saying: READY"
    ),
    "codex": (
        "You are a review agent in a Cookrew cookbook. "
        "You will receive review tasks via this session. "
        "Confirm you are ready by saying: READY"
    ),
}


async def spawn_agent(
    agent_name: str,
    agent_id: str,
    workdir: str = ".",
) -> str | None:
    """Spawn an agent CLI in a tmux session with a greeting prompt.

    Returns the tmux session name if successful, None if tmux not available.
    """
    if not shutil.which("tmux"):
        logger.warning("tmux not found — spawning agent %s as background subprocess", agent_name)
        return await _spawn_subprocess(agent_name, agent_id, workdir)

    session_name = f"{TMUX_SESSION_PREFIX}-{agent_id}"
    greeting = GREETING_PROMPTS.get(agent_name, f"You are agent {agent_name}. Say READY.")

    # Kill existing session if any
    await _run(["tmux", "kill-session", "-t", session_name], ignore_errors=True)

    # Build the agent command
    agent_cmd = _build_agent_command(agent_name, greeting)

    # Create a new detached tmux session running the agent
    result = await _run([
        "tmux", "new-session",
        "-d",                       # detached
        "-s", session_name,         # session name
        "-c", workdir,              # working directory
        agent_cmd,                  # command to run
    ])

    if result is None:
        logger.error("Failed to create tmux session for %s", agent_name)
        return None

    logger.info("Spawned %s in tmux session %s", agent_name, session_name)
    return session_name


async def kill_agent_session(session_name: str) -> bool:
    """Kill a tmux agent session."""
    result = await _run(["tmux", "kill-session", "-t", session_name], ignore_errors=True)
    return result is not None


def kill_agent_session_sync(session_name: str) -> bool:
    """Kill a tmux agent session synchronously (safe during shutdown)."""
    import subprocess
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except Exception:
        return False


async def list_agent_sessions() -> list[str]:
    """List active krew tmux sessions."""
    result = await _run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        ignore_errors=True,
    )
    if result is None:
        return []
    return [
        line.strip()
        for line in result.split("\n")
        if line.strip().startswith(TMUX_SESSION_PREFIX)
    ]


def _build_agent_command(agent_name: str, greeting: str) -> str:
    """Build the shell command to run an agent with a greeting prompt."""
    if agent_name == "claude":
        return f'claude -p "{_escape_for_shell(greeting)}"'
    elif agent_name == "codex":
        return f'codex -q "{_escape_for_shell(greeting)}"'
    else:
        return f'{agent_name} -p "{_escape_for_shell(greeting)}"'


async def _spawn_subprocess(
    agent_name: str, agent_id: str, workdir: str,
) -> str | None:
    """Fallback: spawn agent as a background subprocess (no tmux)."""
    greeting = GREETING_PROMPTS.get(agent_name, f"You are agent {agent_name}. Say READY.")
    cmd = _build_agent_command(agent_name, greeting)

    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Spawned %s as PID %d", agent_name, process.pid)
        return f"pid:{process.pid}"
    except Exception as exc:
        logger.error("Failed to spawn %s: %s", agent_name, exc)
        return None


def _escape_for_shell(text: str) -> str:
    """Escape text for use inside double quotes in a shell command."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


async def _run(cmd: list[str], ignore_errors: bool = False) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            if not ignore_errors:
                logger.warning("Command %s failed: %s", cmd, stderr.decode().strip())
            return None
        return stdout.decode()
    except FileNotFoundError:
        if not ignore_errors:
            logger.warning("Command not found: %s", cmd[0])
        return None
    except (asyncio.CancelledError, KeyboardInterrupt, OSError):
        logger.debug("Command %s interrupted during shutdown", cmd)
        return None
