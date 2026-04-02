from __future__ import annotations

import pytest

from krewcli.storage.interface import TapeContext
from krewcli.storage.tape_client import _build_summary


def test_tape_context_defaults():
    ctx = TapeContext(tape_name="rec_1", summary="")
    assert ctx.entries == []
    assert ctx.last_anchor_id is None


def test_tape_context_with_data():
    ctx = TapeContext(
        tape_name="rec_1",
        summary="Prior work summary",
        entries=[{"kind": "anchor", "payload": {"summary": "Done"}}],
        last_anchor_id=42,
    )
    assert ctx.last_anchor_id == 42
    assert len(ctx.entries) == 1


def test_build_summary_empty():
    assert _build_summary([]) == ""


def test_build_summary_anchor():
    entries = [
        {"kind": "anchor", "payload": {"summary": "Feature X implemented"}},
    ]
    summary = _build_summary(entries)
    assert "[Approved] Feature X implemented" in summary


def test_build_summary_milestone():
    entries = [
        {"kind": "milestone", "payload": {"body": "Added auth endpoint"}},
    ]
    summary = _build_summary(entries)
    assert "[milestone] Added auth endpoint" in summary


def test_build_summary_prompt():
    entries = [
        {"kind": "prompt", "payload": {"body": "Build the login page"}},
    ]
    summary = _build_summary(entries)
    assert "[Request] Build the login page" in summary


def test_build_summary_limits_to_last_10():
    entries = [
        {"kind": "milestone", "payload": {"body": f"Entry {i}"}}
        for i in range(15)
    ]
    summary = _build_summary(entries)
    lines = summary.strip().split("\n")
    assert len(lines) == 10
    # Should include entries 5-14 (last 10)
    assert "Entry 5" in summary
    assert "Entry 14" in summary


def test_build_summary_mixed_types():
    entries = [
        {"kind": "prompt", "payload": {"body": "Build feature Y"}},
        {"kind": "milestone", "payload": {"body": "Created schema"}},
        {"kind": "anchor", "payload": {"summary": "Feature Y approved"}},
        {"kind": "milestone", "payload": {"body": "Started feature Z"}},
    ]
    summary = _build_summary(entries)
    assert "[Request]" in summary
    assert "[milestone]" in summary
    assert "[Approved]" in summary
