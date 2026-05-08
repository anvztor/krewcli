"""Minimal smoke test for the planner sandbox validation path.

The on-disk artifact at ``.krewcli/sandbox_smoke_plan.py`` is a hand-built
GraphBuilder plan that uses only names documented in the planner's
CODEGEN_PROMPT allowlist. This test runs that artifact through the same
``clean_code`` + ``_is_valid_graph_code`` pipeline that the daemon applies
to LLM output, so any drift in the sandbox rules (or the artifact) shows
up immediately.

A companion negative case appends ``import os`` to the artifact and
asserts the sandbox rejects it — confirming the validation path is live
rather than vacuously passing.
"""

from __future__ import annotations

from pathlib import Path

from krewcli.daemon.planner import _is_valid_graph_code, clean_code


SMOKE_PLAN_PATH = (
    Path(__file__).resolve().parent.parent / ".krewcli" / "sandbox_smoke_plan.py"
)


def test_smoke_plan_artifact_exists() -> None:
    assert SMOKE_PLAN_PATH.is_file(), (
        f"Smoke plan artifact missing at {SMOKE_PLAN_PATH}"
    )


def test_smoke_plan_passes_sandbox_validation() -> None:
    code = clean_code(SMOKE_PLAN_PATH.read_text())
    assert _is_valid_graph_code(code), (
        "Sandbox smoke plan rejected by planner validation — "
        "either the artifact drifted or the allowlist tightened."
    )


def test_smoke_plan_with_forbidden_import_is_rejected() -> None:
    tampered = "import os\n" + SMOKE_PLAN_PATH.read_text()
    assert not _is_valid_graph_code(clean_code(tampered)), (
        "Sandbox accepted an import statement — validation path is broken."
    )
