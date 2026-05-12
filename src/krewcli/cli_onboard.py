"""Onboard CLI command — interactive cookbook setup + daemon launch."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click

from krewcli.agents.registry import AGENT_REGISTRY
from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


def register_onboard_command(main: click.Group) -> None:
    """Register the onboard command on the CLI group."""

    @main.command()
    @click.option("--cookbook", default=None, help="Cookbook ID (skips creation)")
    @click.option("--cookbook-name", default="my-cookbook", help="Name for new cookbook")
    @click.option("--owner", default="cli_user", help="Owner ID for cookbook creation")
    @click.option("--workdir", default=None, help="Root working directory (default: ~/krew)")
    @click.option("--agents", default=None, help="Comma-separated agent types (skip selection)")
    @click.option("--max-concurrent", default=1, type=int, help="Max concurrent tasks per agent")
    @click.pass_context
    def onboard(ctx, cookbook, cookbook_name, owner, workdir, agents, max_concurrent):
        """Interactive onboarding — select recipes, agents, and launch daemon.

        \\b
        Steps:
          1. Create or select a cookbook
          2. Clone cookbook repo, select recipes (added as git submodules)
          3. Push submodules back (krewhub auto-indexes)
          4. Detect and select local agents
          5. Launch daemon for task polling

        \\b
        Examples:
          krewcli onboard
          krewcli onboard --cookbook-name my-project --owner drej
          krewcli onboard --cookbook CB_ID --agents claude,codex
        """
        settings = ctx.obj["settings"]
        resolved_workdir = workdir or os.path.join(Path.home(), "krew")
        agent_names = agents.split(",") if agents else None

        asyncio.run(_run_onboard(
            settings=settings,
            cookbook_id=cookbook,
            cookbook_name=cookbook_name,
            owner_id=owner,
            working_dir=resolved_workdir,
            agent_names=agent_names,
            max_concurrent=max_concurrent,
        ))


async def _run_onboard(
    settings,
    cookbook_id,
    cookbook_name,
    owner_id,
    working_dir,
    agent_names,
    max_concurrent,
):
    """Interactive onboarding: clone cookbook, select agents, launch daemon."""
    import shutil

    from krewcli.backend.registry import resolve_backends
    from krewcli.cookbook_repo import (
        clone_or_fetch,
        configure_git_user,
    )
    from krewcli.daemon.loop import DaemonLoop
    from krewcli.interactive import prompt_multi_select

    from krewcli.auth.token_store import load_token as _lt
    client = KrewHubClient(
        settings.krewhub_url, settings.api_key,
        jwt_token=_lt(), verify_ssl=settings.verify_ssl,
    )

    try:
        # 1. Create or reuse cookbook
        if cookbook_id:
            cb = await client.get_cookbook(cookbook_id)
            clone_url = cb.get("clone_url", "")
            click.echo(f"Using cookbook: {cookbook_id}")
        else:
            cb = await client.create_cookbook(name=cookbook_name, owner_id=owner_id)
            cookbook_id = cb["id"]
            clone_url = cb.get("clone_url", "")
            if cb.get("existed"):
                click.echo(f"Reusing cookbook: {cookbook_id}")
            else:
                click.echo(f"Created cookbook: {cookbook_id}")

        if not clone_url:
            click.echo("Error: no clone_url returned for cookbook")
            raise SystemExit(1)

        # 2. Clone cookbook repo
        cookbook_dir = os.path.join(working_dir, cookbook_name)
        click.echo(f"\nCloning cookbook to {cookbook_dir}")
        await clone_or_fetch(clone_url, cookbook_dir)
        await configure_git_user(cookbook_dir, owner_id, f"{owner_id}@krew.local")

        # Recipes were retired in anvztor/krewhub#1 — repos materialize
        # per-bundle via repo_grants now, so onboard no longer adds
        # submodules. Operators bring their working directory or rely
        # on a follow-up repo-grant flow.

        # 3. Detect and select agents
        available_agents = [
            (entry.get("display_name", name), name)
            for name, entry in AGENT_REGISTRY.items()
            if shutil.which(name) is not None
        ]

        if not available_agents:
            click.echo("\nNo agents found on PATH. Using registry defaults.")
            resolved_agent_names = list(AGENT_REGISTRY.keys())[:1]
        elif agent_names is not None:
            resolved_agent_names = agent_names
        else:
            selected_agent_indices = prompt_multi_select("Agents", available_agents)
            resolved_agent_names = [available_agents[i][1] for i in selected_agent_indices]

        # 4. Resolve backends
        backends = resolve_backends(resolved_agent_names)
        if not backends:
            click.echo("No backends available.")
            raise SystemExit(1)

        click.echo(f"\nOnboarding complete:")
        click.echo(f"  Cookbook: {cookbook_id}")
        click.echo(f"  Workspace: {cookbook_dir}")
        click.echo(f"  Agents: {', '.join(backends.keys())}")
        click.echo(f"  KrewHub: {settings.krewhub_url}")

        click.echo(
            f"\nDaemon starting for cookbook {cookbook_id}. Press Ctrl+C to stop.",
        )

        # 5. Launch daemon loop
        daemon = DaemonLoop(
            client=client,
            backends=backends,
            cookbook_id=cookbook_id,
            working_dir=cookbook_dir,
            max_concurrent=max_concurrent,
        )

        try:
            await daemon.run()
        finally:
            await client.close()
            click.echo("Daemon stopped. Goodbye.")

    except Exception as exc:
        try:
            await client.close()
        except Exception:
            pass
        if not isinstance(exc, SystemExit):
            click.echo(f"Onboard failed: {exc}")
            raise SystemExit(1)
        raise
