"""Unit tests for the daemon supervisor (PID file + status sidecar)."""
from __future__ import annotations

import os
import time

import pytest

from krewcli.daemon import supervisor


@pytest.fixture
def isolated_dir(tmp_path, monkeypatch):
    """Redirect supervisor state into a temp directory."""
    monkeypatch.setattr(supervisor, "_DEFAULT_DIR", tmp_path / ".krewcli")
    return tmp_path / ".krewcli"


def test_read_status_returns_none_when_no_pid(isolated_dir):
    assert supervisor.read_status() is None


def test_write_pid_and_read_status_roundtrip(isolated_dir):
    supervisor.write_pid(os.getpid())
    supervisor.write_status({"agents": ["claude"], "ready": True})
    s = supervisor.read_status()
    assert s is not None
    assert s["pid"] == os.getpid()
    assert s["alive"] is True
    assert s["agents"] == ["claude"]
    assert s["ready"] is True


def test_read_status_clears_stale_pid_file(isolated_dir):
    """If the PID file points at a dead process, read_status clears it."""
    # PID 1 is init; we can't kill it, but `os.kill(1, 0)` succeeds for
    # root and raises PermissionError for normal users — both mean
    # "alive". Use an obviously-dead PID instead.
    dead_pid = 2_000_000_000
    supervisor.write_pid(dead_pid)
    assert supervisor.read_status() is None
    assert not supervisor.pid_path().is_file()


def test_update_status_merges_into_existing(isolated_dir):
    supervisor.write_status({"a": 1, "b": 2})
    supervisor.update_status({"b": 20, "c": 3})
    s = supervisor.read_status() or {}
    # No PID written — read_status returns None. Read the file directly.
    import json
    raw = json.loads(supervisor.status_path().read_text())
    assert raw == {"a": 1, "b": 20, "c": 3}


def test_clear_removes_files(isolated_dir):
    supervisor.write_pid(os.getpid())
    supervisor.write_status({"x": 1})
    supervisor.clear()
    assert not supervisor.pid_path().is_file()
    assert not supervisor.status_path().is_file()


def test_wait_until_ready_returns_true_on_ready_marker(isolated_dir):
    """wait_until_ready returns True once the sidecar shows ready=True."""
    supervisor.write_status({"ready": True})
    assert supervisor.wait_until_ready(os.getpid(), timeout=1.0, interval=0.05)


def test_wait_until_ready_returns_false_on_dead_pid(isolated_dir):
    """wait_until_ready bails immediately if the PID is dead."""
    dead = 2_000_000_000
    t0 = time.monotonic()
    assert not supervisor.wait_until_ready(dead, timeout=2.0, interval=0.1)
    # Should have bailed quickly, well before the timeout.
    assert time.monotonic() - t0 < 1.0


def test_spawn_detached_with_trivial_child(isolated_dir, monkeypatch):
    """spawn_detached writes the child PID and returns it."""
    captured: dict = {}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        # Caller passes an open file in stdout; close it to avoid leaks.
        out = kwargs.get("stdout")
        if out and hasattr(out, "close"):
            out.close()
        return _FakeProc(pid=12345)

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = supervisor.spawn_detached(["--cookbook", "cb_x"])
    assert pid == 12345
    assert supervisor.pid_path().read_text() == "12345"

    # Args after `daemon start --foreground` are forwarded verbatim.
    cmd = captured["cmd"]
    assert "daemon" in cmd and "start" in cmd and "--foreground" in cmd
    assert cmd[-2:] == ["--cookbook", "cb_x"]
    # Detach flags set so the child survives the parent shell.
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["close_fds"] is True
