"""In-process codex rollout tailer.

Runs concurrently with a spawned `codex exec` and forwards rollout
events to krewhub as they appear. Replaces the stdout-capture
model — codex's stdout is noisy and often truncates on long runs,
while the rollout JSONL file is codex's own durable, structurally
stable audit log.

Design ported from vibe-island's `CodexSessionWatcher` (see
dump/strings/main-binary-all.txt). Key mechanics:

- `file_offsets`: per-file read cursor so we never re-forward bytes
- `active_files`: sessions we're actively tailing
- `bootstrap_snapshot`: on start, walk existing files and seed
  offsets to their current EOF so we don't replay history
- fast poll (200ms) while a file is growing
- slow poll (2s) fallback when everything is quiet
- `tombstones`: remember sessions we've closed and ignore them for
  a TTL so a stale mtime doesn't re-trigger tailing

Unlike the shim-at-Stop `_replay_codex_rollout` path, this watcher
runs for the entire lifetime of the spawn and forwards events in
real time. The at-Stop replay is kept as a safety net for codex
invocations outside the SpawnManager (e.g. user-initiated `codex`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from krewcli.bridge.canonical import CanonicalHookEvent
from krewcli.bridge.codex_rollout import (
    _event_msg_to_canonical,
    _response_item_to_canonical,
)
from krewcli.bridge.env_collector import collect_env, detect_tty
from krewcli.bridge.forwarder import forward

logger = logging.getLogger(__name__)

# Polling cadence — matches vibe-island's fast/slow split.
FAST_POLL = 0.2
SLOW_POLL = 2.0
ACTIVE_FILE_TIMEOUT = 10.0  # stay in fast-poll mode this long after last write
TOMBSTONE_TTL = 60.0        # remember closed sessions this long


@dataclass
class _FileState:
    """Per-rollout-file bookkeeping."""

    path: Path
    offset: int = 0
    session_id: str = ""
    cwd: str = ""
    last_activity: float = 0.0
    # call_id → (tool_name, args) so function_call_output can pair
    # back to the original PreToolUse for rendering.
    call_id_to_tool: dict[str, tuple[str, dict]] = field(default_factory=dict)


class CodexRolloutWatcher:
    """Tails `$CODEX_HOME/sessions/*.jsonl` in the current process.

    Usage:
        watcher = CodexRolloutWatcher(
            codex_home=codex_home,
            env=hook_env,
            session_id_hint=None,  # optional — filter to one session
        )
        await watcher.start()
        ...
        await watcher.stop()

    The watcher posts events via `bridge.forwarder.forward(..., env=...)`
    so every spawned task's events carry the right KREWHUB_TASK_ID.
    """

    def __init__(
        self,
        *,
        codex_home: str,
        env: dict[str, str],
        session_id_hint: str | None = None,
    ) -> None:
        self._root = Path(codex_home) / "sessions"
        self._env = env
        self._session_filter = session_id_hint
        self._files: dict[Path, _FileState] = {}
        self._tombstones: dict[Path, float] = {}
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._env_snapshot = collect_env()
        self._tty_snapshot = detect_tty()
        self._latest_activity_path: Path | None = None

    @property
    def latest_rollout_path(self) -> Path | None:
        return self._latest_activity_path

    # --------------- lifecycle ---------------

    async def start(self) -> None:
        if self._task is not None:
            return
        # Bootstrap: seed offsets to EOF so we don't replay old sessions.
        self._bootstrap_snapshot()
        self._task = asyncio.create_task(self._run(), name="codex-rollout-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        except Exception:  # noqa: BLE001
            logger.exception("codex watcher: stop() failed")
        finally:
            self._task = None
        # Final drain — in case codex wrote last lines between the
        # final poll and stop().
        try:
            await self._poll_once()
        except Exception:  # noqa: BLE001
            logger.exception("codex watcher: final drain failed")

    # --------------- core loop ---------------

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                any_activity = await self._poll_once()
                delay = FAST_POLL if any_activity else SLOW_POLL
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=delay,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("codex watcher: loop crashed")

    async def _poll_once(self) -> bool:
        """Walk the rollout tree, tail any new content, return True if
        any file actually produced new bytes this tick."""
        any_growth = False
        now = asyncio.get_event_loop().time()

        if not self._root.exists():
            return False

        # Discover new rollout files (or re-discover recent ones).
        for path in self._root.rglob("rollout-*.jsonl"):
            if path in self._tombstones:
                if now - self._tombstones[path] < TOMBSTONE_TTL:
                    continue
                del self._tombstones[path]

            state = self._files.get(path)
            if state is None:
                # New file we've never seen → seed session_id from name.
                sid = _session_id_from_filename(path.name)
                if self._session_filter and sid and sid != self._session_filter:
                    # Not the session we care about — tombstone it
                    # so we don't retry on every tick.
                    self._tombstones[path] = now
                    continue
                state = _FileState(
                    path=path, offset=0, session_id=sid, last_activity=now,
                )
                self._files[path] = state

            grew = await self._drain_file(state)
            if grew:
                any_growth = True
                state.last_activity = now
                self._latest_activity_path = path
            elif now - state.last_activity > ACTIVE_FILE_TIMEOUT:
                # Quiet for a while — drop from active set (it'll be
                # re-picked up by rglob if it grows again).
                pass

        return any_growth

    async def _drain_file(self, state: _FileState) -> bool:
        """Read any new lines from a rollout file, forward canonical events."""
        try:
            stat = state.path.stat()
        except FileNotFoundError:
            return False

        if stat.st_size <= state.offset:
            return False

        try:
            fd = state.path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return False

        try:
            fd.seek(state.offset)
            lines = fd.readlines()
            state.offset = fd.tell()
        finally:
            fd.close()

        forwarded_any = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = self._item_to_canonical(state, item)
            if ev is None:
                continue
            try:
                forward(ev, env=self._env)
                forwarded_any = True
            except Exception:  # noqa: BLE001
                logger.exception("codex watcher: forward failed")
        return forwarded_any

    def _item_to_canonical(
        self,
        state: _FileState,
        item: dict,
    ) -> CanonicalHookEvent | None:
        outer = item.get("type", "")
        payload = item.get("payload")
        if not isinstance(payload, dict):
            return None
        inner = payload.get("type", "")

        if outer == "session_meta":
            # session_meta carries cwd + id; learn them.
            sid = payload.get("id") or state.session_id
            cwd = payload.get("cwd") or state.cwd
            state.session_id = sid
            state.cwd = cwd
            # Emit a session_start-ish canonical event stamped with model info.
            return CanonicalHookEvent(
                hook_event_name="SessionStart",
                source="codex",
                session_id=sid,
                cwd=cwd,
                env=self._env_snapshot,
                tty=self._tty_snapshot,
                extra={
                    "_codex_cli_version": payload.get("cli_version"),
                    "_codex_originator": payload.get("originator"),
                    "_codex_model_provider": payload.get("model_provider"),
                },
            )

        if outer == "event_msg":
            return _event_msg_to_canonical(
                inner, payload, state.session_id, state.cwd,
                self._env_snapshot, self._tty_snapshot,
            )

        if outer == "response_item":
            return _response_item_to_canonical(
                inner, payload, state.session_id, state.cwd,
                self._env_snapshot, self._tty_snapshot,
                state.call_id_to_tool,
            )

        if outer == "turn_context":
            state.cwd = payload.get("cwd") or state.cwd
            return None

        return None

    # --------------- bootstrap ---------------

    def _bootstrap_snapshot(self) -> None:
        """On startup, seed offsets to EOF so we don't replay old content.

        Without this, the watcher would forward the entire history of
        every rollout file it finds on the first poll — millions of
        lines for a long-running codex user.
        """
        if not self._root.exists():
            return
        for path in self._root.rglob("rollout-*.jsonl"):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            sid = _session_id_from_filename(path.name)
            if self._session_filter and sid and sid != self._session_filter:
                self._tombstones[path] = asyncio.get_event_loop().time()
                continue
            self._files[path] = _FileState(
                path=path,
                offset=size,
                session_id=sid,
                last_activity=0.0,
            )


def _session_id_from_filename(name: str) -> str:
    """Extract session UUID from a rollout-<ts>-<session_id>.jsonl filename."""
    stem = name.removesuffix(".jsonl")
    # rollout-<isotimestamp>-<uuid>
    parts = stem.split("-")
    # UUID is 5 dash-separated groups at the end: 8-4-4-4-12
    if len(parts) >= 5:
        return "-".join(parts[-5:])
    return ""
