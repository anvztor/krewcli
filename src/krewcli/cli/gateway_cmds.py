"""Utility commands — status and repo-diagram."""

from __future__ import annotations

from pathlib import Path

import click

from krewcli.agents.registry import AGENT_REGISTRY
from krewcli.repo_diagram import build_repo_diagram


def register_gateway_commands(main: click.Group) -> None:
    """Register status and repo-diagram commands on the CLI group."""

    @main.command()
    @click.pass_context
    def status(ctx):
        """Show available agent backends."""
        for name, entry in AGENT_REGISTRY.items():
            click.echo(f"  {name}: {entry['display_name']}")
            click.echo(f"    capabilities: {', '.join(entry['capabilities'])}")

    @main.command("repo-diagram")
    @click.option("--root", default=".", show_default=True, type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
    @click.option("--format", "diagram_format", default="mermaid", show_default=True, type=click.Choice(["mermaid", "tree"]))
    @click.option("--max-depth", default=3, show_default=True, type=click.IntRange(min=0))
    @click.option("--include-hidden", is_flag=True, default=False, help="Include hidden files and directories.")
    def repo_diagram(root: Path, diagram_format: str, max_depth: int, include_hidden: bool) -> None:
        """Render a repository structure diagram."""
        click.echo(
            build_repo_diagram(
                root=root,
                format=diagram_format,
                max_depth=max_depth,
                include_hidden=include_hidden,
            )
        )
