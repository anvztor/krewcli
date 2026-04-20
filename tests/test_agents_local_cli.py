"""Tests for the refactored ``krewcli.agents.local_cli`` boundary."""

from __future__ import annotations

import asyncio

import pytest

from krewcli.agents import base
from krewcli.agents.base import AgentDeps
from krewcli.agents.local_cli import (
    CommandResult,
    LocalCliAgent,
    _MAX_LINE_CHARS,
    _drain_stream,
    _list_changed_files,
    _read_git_value,
)


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict | None, str]] = []

    async def emit(self, event_type: str, *, payload=None, body: str = "") -> None:
        self.events.append((event_type, payload, body))


@pytest.mark.asyncio
async def test_local_cli_boundary_builds_code_refs_on_failure(monkeypatch):
    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        if args[0] == "broken-cli":
            return CommandResult(1, "", "could not apply patch")
        if args[:2] == ["git", "status"]:
            return CommandResult(0, "R  old.py -> new.py\n?? notes.txt\nX\n", "")
        if args[:3] == ["git", "config", "--get"]:
            return CommandResult(0, "git@example.com:org/repo.git\n", "")
        if args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "abc123\n", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(base, "_run_command", fake_run_command)

    agent = LocalCliAgent(name="Broken", command_builder=lambda prompt: ["broken-cli", prompt])
    result = await agent.run(
        "fix it",
        deps=AgentDeps(working_dir="/tmp", repo_url="", branch="feat/refactor"),
    )

    assert result.output.success is False
    assert result.output.summary == "could not apply patch"
    assert result.output.blocked_reason == "could not apply patch"
    assert result.output.files_modified == ["new.py", "notes.txt"]
    assert result.output.code_refs[0].repo_url == "git@example.com:org/repo.git"
    assert result.output.code_refs[0].commit_sha == "abc123"
    assert result.output.code_refs[0].paths == ["new.py", "notes.txt"]


@pytest.mark.asyncio
async def test_local_cli_boundary_emits_not_installed_when_spawn_fails(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("missing-cli")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    sink = _CollectingSink()
    agent = LocalCliAgent(name="Missing", command_builder=lambda prompt: ["missing-cli", prompt])

    result = await agent.run(
        "hello",
        deps=AgentDeps(working_dir="/tmp", repo_url="", branch="main", event_sink=sink),
    )

    assert result.output.success is False
    assert result.output.blocked_reason == "Missing CLI is not installed"
    assert [event[0] for event in sink.events] == ["session_start", "session_end"]
    assert sink.events[-1][1]["blocked_reason"] == "Missing CLI is not installed"


@pytest.mark.asyncio
async def test_drain_stream_soft_wraps_and_skips_blank_lines():
    reader = asyncio.StreamReader()
    long_line = ("x" * (_MAX_LINE_CHARS + 17)).encode() + b"\n\n"
    reader.feed_data(long_line)
    reader.feed_eof()

    sink = _CollectingSink()
    sink_buffer: list[str] = []

    await _drain_stream(reader, sink_buffer, sink=sink, stream_name="stdout")

    assert sink_buffer == [("x" * (_MAX_LINE_CHARS + 17)) + "\n", "\n"]
    assert [event[1]["block_index"] for event in sink.events] == [0, 1]
    assert sink.events[0][1]["text"] == "x" * _MAX_LINE_CHARS
    assert sink.events[1][1]["text"] == "x" * 17


@pytest.mark.asyncio
async def test_drain_stream_returns_immediately_without_reader():
    sink = _CollectingSink()
    sink_buffer: list[str] = []

    await _drain_stream(None, sink_buffer, sink=sink, stream_name="stderr")

    assert sink_buffer == []
    assert sink.events == []


@pytest.mark.asyncio
async def test_drain_stream_without_sink_still_buffers():
    """Line 352-353: when sink is None, lines are buffered but no events emitted."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"hello\nworld\n")
    reader.feed_eof()

    sink_buffer: list[str] = []
    await _drain_stream(reader, sink_buffer, sink=None, stream_name="stdout")

    assert sink_buffer == ["hello\n", "world\n"]


@pytest.mark.asyncio
async def test_drain_stream_swallows_emit_exception():
    """Lines 374-375: emit failures are swallowed, drain continues."""

    class _BrokenSink:
        async def emit(self, event_type, *, payload=None, body=""):
            raise RuntimeError("emit boom")

    reader = asyncio.StreamReader()
    reader.feed_data(b"line1\nline2\n")
    reader.feed_eof()

    sink_buffer: list[str] = []
    await _drain_stream(reader, sink_buffer, sink=_BrokenSink(), stream_name="stdout")

    assert sink_buffer == ["line1\n", "line2\n"]


@pytest.mark.asyncio
async def test_drain_stream_handles_decode_error():
    """Lines 347-348: non-decodable bytes fall back to repr."""
    reader = asyncio.StreamReader()
    # Feed raw bytes that will be decoded with errors="replace"
    reader.feed_data(b"\xff\xfe\n")
    reader.feed_eof()

    sink = _CollectingSink()
    sink_buffer: list[str] = []
    await _drain_stream(reader, sink_buffer, sink=sink, stream_name="stdout")

    assert len(sink_buffer) == 1
    # Should have decoded (with replacements) rather than crashing
    assert sink_buffer[0].endswith("\n")


@pytest.mark.asyncio
async def test_list_changed_files_parses_renames(monkeypatch):
    async def fake_read_git_value(args, working_dir, *, allow_empty=False):
        assert allow_empty is True
        return "M  src/app.py\nR  old.py -> new.py\n?? notes.txt\nX\n"

    monkeypatch.setattr(base, "_read_git_value", fake_read_git_value)

    assert await _list_changed_files("/tmp/work") == ["src/app.py", "new.py", "notes.txt"]


@pytest.mark.asyncio
async def test_read_git_value_returns_empty_when_command_missing(monkeypatch):
    async def fake_run_command(args, working_dir, *, timeout=30):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(base, "_run_command", fake_run_command)

    assert await _read_git_value(["git", "status"], "/tmp/work") == ""


@pytest.mark.asyncio
async def test_read_git_value_returns_empty_for_nonzero_exit(monkeypatch):
    async def fake_run_command(args, working_dir, *, timeout=30):
        return CommandResult(1, "ignored", "boom")

    monkeypatch.setattr(base, "_run_command", fake_run_command)

    assert await _read_git_value(["git", "status"], "/tmp/work") == ""


# ---------------------------------------------------------------------------
# _run_command timeout coverage (lines 436-449)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_command_timeout_kills_process(monkeypatch):
    """When _run_command times out it kills the process group and re-raises."""
    from krewcli.agents.base import _run_command

    killed = {"pgid": False, "proc": False}

    class _FakeProcess:
        pid = 12345
        returncode = None

        async def communicate(self):
            # First call: simulate timeout; second call: return empty
            if not killed["pgid"]:
                await asyncio.sleep(999)
            return b"", b""

        def kill(self):
            killed["proc"] = True
            self.returncode = -9

        async def wait(self):
            return -9

    async def _fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    # killpg succeeds → process group killed
    def _fake_killpg(pid, sig):
        killed["pgid"] = True

    import os as _os
    monkeypatch.setattr(_os, "killpg", _fake_killpg)

    with pytest.raises(asyncio.TimeoutError):
        await _run_command(["fake"], "/tmp", timeout=0)

    assert killed["pgid"] is True


@pytest.mark.asyncio
async def test_run_command_timeout_fallback_when_process_already_gone(monkeypatch):
    """When killpg raises ProcessLookupError, falls back to process.kill()."""
    from krewcli.agents.base import _run_command

    killed = {"proc": False}

    class _FakeProcess:
        pid = 99999
        returncode = None

        async def communicate(self):
            if not killed["proc"]:
                await asyncio.sleep(999)
            return b"", b""

        def kill(self):
            killed["proc"] = True
            self.returncode = -9

        async def wait(self):
            return -9

    async def _fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    import os as _os
    def _raise_lookup(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(_os, "killpg", _raise_lookup)

    with pytest.raises(asyncio.TimeoutError):
        await _run_command(["fake"], "/tmp", timeout=0)

    assert killed["proc"] is True


# ---------------------------------------------------------------------------
# LocalCliAgent.run timeout with sink (lines 210-256)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_cli_agent_timeout_with_sink_emits_session_end(monkeypatch):
    """When subprocess times out and event_sink is set, emits session_end with timeout info."""
    wait_count = 0

    class _HangingProcess:
        pid = 7777
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 1:
                # First call: hang until cancelled by wait_for timeout
                await asyncio.sleep(999)
            # Subsequent calls: return immediately (after kill)
            self.returncode = -9

        def kill(self):
            self.returncode = -9

    _proc = _HangingProcess()

    async def _fake_exec(*args, **kwargs):
        return _proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    import os as _os
    def _fake_killpg(pid, sig):
        _proc.returncode = -9

    monkeypatch.setattr(_os, "killpg", _fake_killpg)

    sink = _CollectingSink()
    agent = LocalCliAgent(name="Slow", command_builder=lambda p: ["slow-cli", p])

    from krewcli.agents.base import HarnessConfig
    result = await agent.run(
        "work",
        deps=AgentDeps(
            working_dir="/tmp", repo_url="", branch="main",
            event_sink=sink,
            harness=HarnessConfig(timeout=1),
        ),
    )

    assert result.output.success is False
    assert "timed out" in result.output.summary
    event_types = [e[0] for e in sink.events]
    assert "session_start" in event_types
    assert "session_end" in event_types
    end_payload = [e[1] for e in sink.events if e[0] == "session_end"][0]
    assert end_payload["success"] is False
    assert end_payload["blocked_reason"] == "timeout"


# ---------------------------------------------------------------------------
# _drain_stream: generic read exception (lines 336-340)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_stream_stops_on_generic_read_error():
    """When readline raises a non-cancelled exception, drain stops cleanly."""

    class _ExplodingReader:
        _count = 0

        async def readline(self):
            self._count += 1
            if self._count == 1:
                return b"first line\n"
            raise OSError("disk exploded")

    sink = _CollectingSink()
    sink_buffer: list[str] = []
    await _drain_stream(_ExplodingReader(), sink_buffer, sink=sink, stream_name="stdout")

    assert sink_buffer == ["first line\n"]
