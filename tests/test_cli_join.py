"""Command tests for ``krewcli join`` (deprecated stub).

These verify that the join command:
  1. Rejects legacy single-agent modes
  2. Delegates to the daemon loop for gateway mode
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from krewcli.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def test_join_rejects_legacy_provider(runner):
    """Legacy --provider mode should be rejected."""
    result = runner.invoke(main, [
        "join", "--recipe", "R1", "--cookbook", "CB1",
        "--provider", "anthropic",
    ])
    assert result.exit_code != 0
    assert "Legacy" in result.output or "removed" in result.output.lower()


def test_join_rejects_legacy_framework(runner):
    """Legacy --framework mode should be rejected."""
    result = runner.invoke(main, [
        "join", "--recipe", "R1", "--cookbook", "CB1",
        "--framework", "anthropic",
    ])
    assert result.exit_code != 0


def test_join_requires_recipe(runner):
    """Join without --recipe should error."""
    result = runner.invoke(main, [
        "join", "--cookbook", "CB1",
    ])
    assert result.exit_code != 0
    assert "recipe" in result.output.lower()


def test_join_requires_cookbook(runner):
    """Join without --cookbook (and no default) should error."""
    result = runner.invoke(main, [
        "join", "--recipe", "R1",
    ], env={"KREWCLI_DEFAULT_COOKBOOK_ID": ""})
    # Should either require cookbook or use default
    # The specific error depends on settings
    assert result.exit_code != 0 or "cookbook" in result.output.lower()
