"""Terminal/environment context collection.

Lifted from vibe-island's opencode hook adapter — collects the
fixed allowlist of env vars that identify the user's terminal,
multiplexer, IDE, and CMUX/Conductor workspace, plus walks the
process tree upward to find the actual TTY (because subprocess
runners typically have no tty themselves).
"""

from __future__ import annotations

import os
import subprocess

# Verbatim from vibe-island. Do not extend lightly — these are the
# correlation keys consumers (terminal jumpers, session UIs) expect.
ENV_KEYS: tuple[str, ...] = (
    "TERM_PROGRAM",
    "ITERM_SESSION_ID",
    "TERM_SESSION_ID",
    "TMUX",
    "TMUX_PANE",
    "KITTY_WINDOW_ID",
    "__CFBundleIdentifier",
    "CONDUCTOR_WORKSPACE_NAME",
    "CONDUCTOR_PORT",
    "CURSOR_TRACE_ID",
    "CMUX_WORKSPACE_ID",
    "CMUX_SURFACE_ID",
    "CMUX_SOCKET_PATH",
)


def collect_env() -> dict[str, str]:
    """Return the env-var allowlist values present in os.environ."""
    return {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


def detect_tty(max_hops: int = 8) -> str | None:
    """Walk the process tree upward to find a real TTY.

    Subprocess runners (Bun workers, Python child procs) usually
    have no tty themselves; the user's actual terminal sits 2-5
    process levels up. Vibe-island walks at most 8 hops.
    """
    pid = os.getpid()
    for _ in range(max_hops):
        try:
            out = subprocess.check_output(
                ["ps", "-o", "tty=,ppid=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                timeout=1,
            ).decode().strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None

        parts = out.split()
        if len(parts) < 2:
            return None
        tty, ppid_raw = parts[0], parts[1]
        if tty and tty not in ("?", "??"):
            return f"/dev/{tty}"
        try:
            ppid = int(ppid_raw)
        except ValueError:
            return None
        if ppid <= 1:
            return None
        pid = ppid
    return None
