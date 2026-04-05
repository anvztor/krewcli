"""Tests for interactive prompt helpers."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from krewcli.interactive import prompt_multi_select, prompt_single_select


# ---------------------------------------------------------------------------
# prompt_multi_select
# ---------------------------------------------------------------------------

def test_multi_select_all_default():
    """Default 'all' selects everything."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b"), ("Gamma", "c")]

    @click.command()
    def cmd():
        result = prompt_multi_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="all\n")
    assert "selected=[0, 1, 2]" in result.output


def test_multi_select_specific():
    """Comma-separated numbers select specific items."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b"), ("Gamma", "c")]

    @click.command()
    def cmd():
        result = prompt_multi_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="1,3\n")
    assert "selected=[0, 2]" in result.output


def test_multi_select_empty_returns_all():
    """No valid selection defaults to all."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b")]

    @click.command()
    def cmd():
        result = prompt_multi_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="x\n")
    assert "selected=[0, 1]" in result.output


def test_multi_select_out_of_range():
    """Out-of-range numbers are ignored."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b")]

    @click.command()
    def cmd():
        result = prompt_multi_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="1,99\n")
    assert "selected=[0]" in result.output
    assert "Ignoring out-of-range: 99" in result.output


def test_multi_select_empty_list():
    """Empty items list returns empty selection."""
    result = prompt_multi_select("Test", [])
    assert result == []


def test_multi_select_deduplicates():
    """Duplicate selections are deduplicated."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b")]

    @click.command()
    def cmd():
        result = prompt_multi_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="1,1,2\n")
    assert "selected=[0, 1]" in result.output


# ---------------------------------------------------------------------------
# prompt_single_select
# ---------------------------------------------------------------------------

def test_single_select_auto_selects_single():
    """Single item is auto-selected."""
    items = [("Alpha", "a")]
    result = prompt_single_select("Test", items)
    assert result == 0


def test_single_select_specific():
    """Choosing a number selects that item."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b"), ("Gamma", "c")]

    @click.command()
    def cmd():
        result = prompt_single_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="2\n")
    assert "selected=1" in result.output


def test_single_select_empty_raises():
    """Empty list raises UsageError."""
    with pytest.raises(click.UsageError):
        prompt_single_select("Test", [])


def test_single_select_retries_on_invalid():
    """Invalid input prompts again until valid."""
    runner = CliRunner()
    items = [("Alpha", "a"), ("Beta", "b")]

    @click.command()
    def cmd():
        result = prompt_single_select("Test", items)
        click.echo(f"selected={result}")

    result = runner.invoke(cmd, input="x\n1\n")
    assert "selected=0" in result.output
    assert "Please enter a number" in result.output
