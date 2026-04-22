"""Join and start commands — deprecated, delegates to daemon start.

The old A2A-based join workflow is replaced by the managed agent daemon.
These commands are kept as aliases for backward compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import os

import click

from krewcli.agents.registry import AGENT_REGISTRY
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.presence.heartbeat import HeartbeatLoop  # noqa: F401 — re-exported via cli/__init__

logger = logging.getLogger(__name__)


def _default_model(provider: str) -> str:
    return {"anthropic": "claude-sonnet-4-20250514", "openai": "gpt-4o"}.get(
        provider, "claude-sonnet-4-20250514"
    )


def _resolve_mode(*_args, **_kwargs):
    raise click.UsageError(
        "Legacy single-agent modes (--provider, --framework, --endpoint, --orchestrator) "
        "are no longer supported. Use 'krewcli daemon start' instead."
    )


async def _run_agent(*_args, **_kwargs):
    raise click.UsageError(
        "Legacy single-agent mode removed. Use 'krewcli daemon start' instead."
    )


async def _run_gateway(
    settings, recipe_id, cookbook_id, agent_id_prefix, working_dir,
    agent_names, max_concurrent,
):
    """Delegate to new daemon loop."""
    from krewcli.backend.registry import resolve_backends
    from krewcli.daemon.loop import DaemonLoop
    from krewcli.auth.token_store import load_token

    jwt_token = load_token()
    client = KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=jwt_token,
        verify_ssl=settings.verify_ssl,
    )

    backends = resolve_backends(agent_names)
    if not backends:
        raise click.ClickException("No backends available.")

    loop = DaemonLoop(
        client=client,
        backends=backends,
        cookbook_id=cookbook_id,
        recipe_id=recipe_id,
        working_dir=working_dir,
        max_concurrent=max_concurrent,
    )

    try:
        await loop.run()
    finally:
        await client.close()


def register_join_commands(main: click.Group) -> None:
    """Register join and start commands on the CLI group."""

    @main.command()
    @click.option("--recipe", default=None, help="Recipe ID")
    @click.option("--cookbook", default=None, help="Cookbook ID")
    @click.option("--port", default=9999, type=int, hidden=True)
    @click.option("--agent-id", default=None, hidden=True)
    @click.option("--workdir", default=".", help="Working directory")
    @click.option("--agents", default=None, help="Comma-separated backend names")
    @click.option("--max-concurrent", default=1, type=int, help="Max concurrent tasks")
    @click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default=None, hidden=True)
    @click.option("--provider", default=None, hidden=True)
    @click.option("--model", default=None, hidden=True)
    @click.option("--endpoint", default=None, hidden=True)
    @click.option("--framework", default=None, hidden=True)
    @click.option("--orchestrator", is_flag=True, default=False, hidden=True)
    @click.pass_context
    def join(ctx, recipe, cookbook, port, agent_id, workdir, agents, max_concurrent,
             agent, provider, model, endpoint, framework, orchestrator):
        """Bring agents online. (Delegates to daemon start)

        \b
        Use 'krewcli daemon start' directly for the new managed agent workflow.
        This command is kept for backward compatibility.
        """
        settings = ctx.obj["settings"]

        # Reject legacy single-agent modes
        if any([provider, model, endpoint, framework, orchestrator]):
            raise click.UsageError(
                "Legacy modes (--provider/--framework/--endpoint/--orchestrator) removed.\n"
                "Use 'krewcli daemon start --cookbook CB --recipe ID' instead."
            )

        resolved_workdir = os.path.abspath(workdir)
        resolved_cookbook = cookbook or settings.default_cookbook_id

        if not resolved_cookbook:
            raise click.UsageError("Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID")
        if not recipe:
            raise click.UsageError("Specify --recipe")

        agent_names = agents.split(",") if agents else (
            [agent] if agent else None
        )

        click.echo("[deprecated] 'krewcli join' now delegates to the daemon loop.")
        click.echo("Use 'krewcli daemon start' directly.\n")

        asyncio.run(_run_gateway(
            settings=settings,
            recipe_id=recipe,
            cookbook_id=resolved_cookbook,
            agent_id_prefix=agent_id or f"gw_{os.getpid()}",
            working_dir=resolved_workdir,
            agent_names=agent_names,
            max_concurrent=max_concurrent,
        ))

    @main.command()
    @click.option("--recipe", required=True)
    @click.option("--cookbook", default=None)
    @click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default="claude")
    @click.option("--agent-id", default=None, hidden=True)
    @click.option("--port", default=9999, type=int, hidden=True)
    @click.option("--workdir", default=".")
    @click.pass_context
    def start(ctx, recipe, cookbook, agent, agent_id, port, workdir):
        """[Legacy] Start an agent. Use 'daemon start' instead."""
        ctx.invoke(join, recipe=recipe, cookbook=cookbook, agent=agent,
                   agent_id=agent_id, workdir=workdir)
