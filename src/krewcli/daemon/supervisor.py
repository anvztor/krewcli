"""Daemon supervisor — spawn/stop/status helpers for the background daemon.

Mirrors multica's pattern (server/cmd/multica/cmd_daemon.go): the
foreground command (``krewcli login`` or ``krewcli daemon start
--background``) forks a child running ``krewcli daemon start
--foreground <args>`` and detaches. The child writes
``~/.krewcli/daemon.pid`` + ``daemon.json`` with its config; the parent
exits.

Multica's daemon also exposes a /health HTTP endpoint that the desktop
app polls. krewcli is CLI-only, so we keep it lighter: the PID file
proves liveness, and the daemon writes a JSON status sidecar at startup
+ on every heartbeat tick. ``daemon status`` reads that sidecar and
confirms the PID is alive.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_DEFAULT_DIR = Path.home() / ".krewcli"


def _dir() -> Path:
    return _DEFAULT_DIR


def pid_path() -> Path:
    return _dir() / "daemon.pid"


def status_path() -> Path:
    return _dir() / "daemon.json"


def log_path() -> Path:
    return _dir() / "daemon.log"


def _is_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live process owned by us."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — treat as alive.
        return True
    except OSError:
        return False
    return True


def write_pid(pid: int) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    pid_path().write_text(str(pid), encoding="utf-8")


def write_status(meta: dict) -> None:
    """Persist daemon metadata (cookbook, recipe, agents, started_at)."""
    _dir().mkdir(parents=True, exist_ok=True)
    status_path().write_text(json.dumps(meta), encoding="utf-8")


def update_status(updates: dict) -> None:
    """Merge ``updates`` into the existing status file (no-op if missing)."""
    path = status_path()
    if not path.is_file():
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        current = {}
    current.update(updates)
    try:
        path.write_text(json.dumps(current), encoding="utf-8")
    except OSError:
        pass


def read_status() -> dict | None:
    """Return ``{pid, alive, ...meta}`` for a running daemon, else ``None``.

    Side effect: clears stale PID files (PID exists but process is gone).
    """
    path = pid_path()
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        clear()
        return None
    if not _is_alive(pid):
        clear()
        return None
    meta: dict = {}
    s = status_path()
    if s.is_file():
        try:
            meta = json.loads(s.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            meta = {}
    return {"pid": pid, "alive": True, **meta}


def clear() -> None:
    """Remove the PID + status files (best-effort)."""
    for f in (pid_path(), status_path()):
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass


def spawn_detached(daemon_args: list[str]) -> int:
    """Spawn ``krewcli daemon start --foreground <args>`` detached.

    Returns the child PID. Stdin is closed; stdout/stderr stream to
    ``~/.krewcli/daemon.log`` (append). Uses ``start_new_session=True``
    so the child survives the parent shell.
    """
    _dir().mkdir(parents=True, exist_ok=True)
    log_file = log_path().open("ab")
    try:
        cmd = [
            sys.executable, "-m", "krewcli",
            "daemon", "start", "--foreground", *daemon_args,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_file.close()
    write_pid(proc.pid)
    return proc.pid


def wait_until_ready(pid: int, timeout: float = 15.0, interval: float = 0.3) -> bool:
    """Block until the daemon writes its status file (proof it bootstrapped).

    Returns True if the daemon registered itself within ``timeout``.
    Returns False if the process died first or didn't write status in time.
    """
    deadline = time.monotonic() + timeout
    s = status_path()
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return False
        if s.is_file():
            try:
                meta = json.loads(s.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
            # `ready=True` is set by DaemonLoop after agents are registered.
            if meta.get("ready"):
                return True
        time.sleep(interval)
    return _is_alive(pid)


def stop(timeout: float = 10.0) -> bool:
    """SIGTERM the running daemon, fall back to SIGKILL after ``timeout``.

    Returns True if a daemon was running and is now stopped.
    """
    status = read_status()
    if status is None:
        return False
    pid = int(status["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear()
        return False
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            clear()
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    clear()
    return True
