"""CLI commands for the managed agent daemon.

``krewcli daemon start`` replaces the old ``krewcli join --gateway``
workflow with a simpler pull-based daemon that polls krewhub for tasks.

When ``--cookbook`` and ``--recipe`` are omitted, interactive prompts
guide the user through cookbook/recipe/agent selection — preserving
the UX from the old ``join`` command.
"""

from __future__ import annotations

import asyncio
import shutil

import click

from krewcli.client.krewhub_client import KrewHubClient


def register_daemon_commands(group: click.Group) -> None:
    """Register the ``daemon`` command group."""
    group.add_command(daemon)


@click.group()
def daemon() -> None:
    """Manage the krewcli daemon."""


@daemon.command()
@click.option("--cookbook", default=None, help="Cookbook ID (interactive if omitted)")
@click.option("--recipe", default=None, help="Recipe ID (interactive if omitted)")
@click.option("--workdir", default=".", help="Working directory for agent execution")
@click.option(
    "--agents",
    default=None,
    help="Comma-separated backend names (interactive if omitted). e.g. claude,codex,echo",
)
@click.option("--max-concurrent", default=1, type=int, help="Max concurrent task executions")
@click.option("--poll-interval", default=5.0, type=float, help="Seconds between polls")
@click.option("--repo-url", default="", help="Repository URL for code ref tracking")
@click.option("--branch", default="", help="Branch name for code ref tracking")
@click.pass_context
def start(
    ctx: click.Context,
    cookbook: str | None,
    recipe: str | None,
    workdir: str,
    agents: str | None,
    max_concurrent: int,
    poll_interval: float,
    repo_url: str,
    branch: str,
) -> None:
    """Start the daemon. Polls krewhub for tasks and executes them.

    \b
    When --cookbook or --recipe are omitted, an interactive prompt
    guides you through selecting them from your krewhub account.

    \b
    Examples:
      krewcli daemon start                           # fully interactive
      krewcli daemon start --cookbook CB --recipe R   # non-interactive
      krewcli daemon start --cookbook CB --recipe R --agents echo  # test mode
    """
    import os

    from krewcli.backend.registry import resolve_backends, BACKEND_INFO
    from krewcli.interactive import prompt_multi_select, prompt_single_select

    settings = ctx.obj["settings"]
    resolved_workdir = os.path.abspath(workdir)

    resolved_cookbook = cookbook or settings.default_cookbook_id
    resolved_recipe = recipe
    need_interactive = not resolved_cookbook or not resolved_recipe

    # ── Interactive selection (uses a temporary sync client) ──────

    if need_interactive:
        # Use a dedicated event loop + client for interactive fetches
        # so it doesn't conflict with the daemon's loop later.
        _loop = asyncio.new_event_loop()
        _client = _make_sync_client(settings)

        try:
            if not resolved_cookbook:
                cookbooks = _loop.run_until_complete(_client.list_cookbooks())
                if not cookbooks:
                    raise click.ClickException(
                        "No cookbooks found. Create one in cookrew first."
                    )
                cb_items = [(cb["name"], cb["id"]) for cb in cookbooks]
                cb_idx = prompt_single_select("Cookbooks", cb_items)
                resolved_cookbook = cookbooks[cb_idx]["id"]

            if not resolved_recipe:
                detail = _loop.run_until_complete(
                    _client.get_cookbook(resolved_cookbook),
                )
                recipes = detail.get("recipes", [])
                if not recipes:
                    raise click.ClickException(
                        f"No recipes in cookbook '{resolved_cookbook}'. "
                        "Create one in cookrew first."
                    )
                rec_items = [(r.get("name", r["id"]), r["id"]) for r in recipes]
                if len(rec_items) == 1:
                    rec_idx = 0
                    click.echo(
                        f"\nRecipe: {rec_items[0][0]} ({rec_items[0][1]}) [auto-selected]"
                    )
                else:
                    rec_idx = prompt_single_select("Recipes", rec_items)
                resolved_recipe = recipes[rec_idx]["id"]
        finally:
            _loop.run_until_complete(_client.close())
            _loop.close()

    # Agent selection
    if agents is not None:
        requested = [a.strip() for a in agents.split(",")]
    else:
        detected = [
            name for name in BACKEND_INFO
            if name == "echo" or shutil.which(name) is not None
        ]
        if not detected:
            raise click.ClickException(
                "No agent CLIs found on PATH (claude, codex, bub). "
                "Install one or use --agents echo for testing."
            )
        agent_items = [
            (BACKEND_INFO.get(n, {}).get("display_name", n), n)
            for n in detected
        ]
        selected_indices = prompt_multi_select("Agents (detected on PATH)", agent_items)
        requested = [detected[i] for i in selected_indices]

    backends = resolve_backends(requested)
    if not backends:
        raise click.ClickException("No backends resolved.")

    # ── Start the daemon (single asyncio.run for the lifetime) ───

    click.echo("\nkrewcli daemon starting")
    click.echo(f"  KrewHub:    {settings.krewhub_url}")
    click.echo(f"  Cookbook:    {resolved_cookbook}")
    click.echo(f"  Recipe:     {resolved_recipe}")
    click.echo(f"  Agents:     {', '.join(backends.keys())}")
    click.echo(f"  Work dir:   {resolved_workdir}")
    click.echo(f"  Concurrent: {max_concurrent}")

    # Create a fresh client for the daemon event loop.
    asyncio.run(_run_daemon(
        settings=settings,
        backends=backends,
        cookbook_id=resolved_cookbook,
        recipe_id=resolved_recipe,
        working_dir=resolved_workdir,
        repo_url=repo_url,
        branch=branch,
        max_concurrent=max_concurrent,
        poll_interval=poll_interval,
    ))


async def _run_daemon(
    settings,
    backends,
    cookbook_id: str,
    recipe_id: str,
    working_dir: str,
    repo_url: str,
    branch: str,
    max_concurrent: int,
    poll_interval: float,
) -> None:
    """Create a fresh client and run the daemon loop."""
    from krewcli.auth.token_store import load_token
    from krewcli.daemon.loop import DaemonLoop

    jwt_token = load_token()
    client = KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=jwt_token,
        verify_ssl=settings.verify_ssl,
    )

    loop = DaemonLoop(
        client=client,
        backends=backends,
        cookbook_id=cookbook_id,
        recipe_id=recipe_id,
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        max_concurrent=max_concurrent,
        poll_interval=poll_interval,
    )

    try:
        await loop.run()
    except KeyboardInterrupt:
        click.echo("\nDaemon stopped.")
    finally:
        await client.close()


@daemon.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show daemon status (agents, running tasks)."""
    click.echo("Daemon status check not yet implemented for pull-based daemon.")
    click.echo("Use krewhub agent presence API to check registered agents.")


# ── Helpers ──────────────────────────────────────────────────────


def _make_sync_client(settings) -> KrewHubClient:
    """Create a temporary KrewHubClient for interactive selection.

    Uses a separate instance so it doesn't interfere with the daemon's
    event loop. Callers must close it when done.
    """
    from krewcli.auth.token_store import load_token
    return KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=load_token(),
        verify_ssl=settings.verify_ssl,
    )


