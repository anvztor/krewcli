from __future__ import annotations

import asyncio
import logging
import os

import click
import uvicorn

from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.config import get_settings
from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.workflow.task_runner import TaskRunner
from krewcli.workflow.digest_builder import DigestBuilder


@click.group()
@click.pass_context
def main(ctx: click.Context) -> None:
    """KrewCLI — A2A agent server for Cookrew."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    settings = get_settings()
    ctx.obj["settings"] = settings
    ctx.obj["client"] = KrewHubClient(settings.krewhub_url, settings.api_key)


@main.command()
@click.option("--recipe", required=True, help="Recipe ID to join")
@click.option(
    "--agent",
    type=click.Choice(list(AGENT_REGISTRY.keys())),
    default="claude",
    help="Agent backend to use",
)
@click.option("--agent-id", default=None, help="Override agent ID")
@click.option("--workdir", default=".", help="Working directory for agent")
@click.pass_context
def start(
    ctx: click.Context,
    recipe: str,
    agent: str,
    agent_id: str | None,
    workdir: str,
) -> None:
    """Start the A2A agent server with heartbeat to KrewHub."""
    settings = ctx.obj["settings"]
    info = get_agent_info(agent)
    resolved_agent_id = agent_id or f"{agent}_{os.getpid()}"
    resolved_workdir = os.path.abspath(workdir)

    click.echo(f"Starting KrewCLI agent server")
    click.echo(f"  Agent: {info['display_name']} ({resolved_agent_id})")
    click.echo(f"  Recipe: {recipe}")
    click.echo(f"  Work dir: {resolved_workdir}")
    click.echo(f"  A2A endpoint: http://{settings.agent_host}:{settings.agent_port}")
    click.echo(f"  KrewHub: {settings.krewhub_url}")

    asyncio.run(_run_server(
        settings=settings,
        recipe_id=recipe,
        agent_name=agent,
        agent_id=resolved_agent_id,
        working_dir=resolved_workdir,
    ))


async def _run_server(
    settings,
    recipe_id: str,
    agent_name: str,
    agent_id: str,
    working_dir: str,
) -> None:
    from krewcli.a2a.server import create_a2a_app

    client = KrewHubClient(settings.krewhub_url, settings.api_key)
    info = get_agent_info(agent_name)

    heartbeat = HeartbeatLoop(
        client=client,
        agent_id=agent_id,
        recipe_id=recipe_id,
        display_name=info["display_name"],
        capabilities=info["capabilities"],
        interval=settings.heartbeat_interval,
    )
    heartbeat.start()

    a2a_app = create_a2a_app(
        host=settings.agent_host,
        port=settings.agent_port,
        default_agent=agent_name,
        active_agents=[agent_name],
        working_dir=working_dir,
    )

    config = uvicorn.Config(
        a2a_app.build(),
        host=settings.agent_host,
        port=settings.agent_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await heartbeat.stop()
        await client.close()


@main.command("list-tasks")
@click.option("--recipe", required=True, help="Recipe ID")
@click.pass_context
def list_tasks(ctx: click.Context, recipe: str) -> None:
    """List available tasks for a recipe."""
    client: KrewHubClient = ctx.obj["client"]

    async def _run():
        try:
            bundles = await client.list_bundles(recipe)
            for b in bundles:
                if b["status"] in ("open", "claimed"):
                    bundle = await client.get_bundle(b["id"])
                    tasks = bundle.get("tasks", [])
                    click.echo(f"\nBundle: {b['id']} [{b['status']}]")
                    click.echo(f"  Prompt: {b['prompt'][:80]}")
                    for t in tasks:
                        status_icon = {
                            "open": "[ ]",
                            "claimed": "[>]",
                            "working": "[~]",
                            "done": "[x]",
                            "blocked": "[!]",
                            "cancelled": "[-]",
                        }.get(t["status"], "[?]")
                        agent = t.get("claimed_by_agent_id", "")
                        agent_str = f" ({agent})" if agent else ""
                        click.echo(f"    {status_icon} {t['id']}: {t['title']}{agent_str}")
        finally:
            await client.close()

    asyncio.run(_run())


@main.command()
@click.argument("task_id")
@click.option("--recipe", required=True, help="Recipe ID")
@click.option(
    "--agent",
    type=click.Choice(list(AGENT_REGISTRY.keys())),
    default="claude",
)
@click.option("--agent-id", default=None)
@click.option("--workdir", default=".")
@click.pass_context
def claim(
    ctx: click.Context,
    task_id: str,
    recipe: str,
    agent: str,
    agent_id: str | None,
    workdir: str,
) -> None:
    """Claim and execute a task."""
    client: KrewHubClient = ctx.obj["client"]
    settings = ctx.obj["settings"]
    resolved_agent_id = agent_id or f"{agent}_{os.getpid()}"
    info = get_agent_info(agent)

    async def _run():
        heartbeat = HeartbeatLoop(
            client=client,
            agent_id=resolved_agent_id,
            recipe_id=recipe,
            display_name=info["display_name"],
            capabilities=info["capabilities"],
            interval=settings.heartbeat_interval,
        )
        heartbeat.start()

        runner = TaskRunner(
            client=client,
            heartbeat=heartbeat,
            agent_name=agent,
            agent_id=resolved_agent_id,
            working_dir=os.path.abspath(workdir),
            repo_url="",
            branch="main",
        )

        try:
            result = await runner.claim_and_execute(task_id)
            if result:
                click.echo(f"Task {task_id} completed: {result.summary}")
            else:
                click.echo(f"Task {task_id} failed or could not be claimed")
        finally:
            await heartbeat.stop()
            await client.close()

    asyncio.run(_run())


@main.command()
@click.argument("task_id")
@click.option("--body", required=True, help="Milestone description")
@click.option("--fact", multiple=True, help="Fact claims (repeatable)")
@click.option("--agent-id", default="cli_user")
@click.pass_context
def milestone(
    ctx: click.Context,
    task_id: str,
    body: str,
    fact: tuple[str, ...],
    agent_id: str,
) -> None:
    """Post a milestone event to a task."""
    client: KrewHubClient = ctx.obj["client"]

    async def _run():
        try:
            facts = [
                {"id": f"f_{i}", "claim": f, "captured_by": agent_id}
                for i, f in enumerate(fact)
            ]
            event = await client.post_event(
                task_id=task_id,
                event_type="milestone",
                actor_id=agent_id,
                body=body,
                facts=facts,
            )
            click.echo(f"Milestone posted: {event['id']}")
        finally:
            await client.close()

    asyncio.run(_run())


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show available agent backends."""
    for name, entry in AGENT_REGISTRY.items():
        click.echo(f"  {name}: {entry['display_name']}")
        click.echo(f"    capabilities: {', '.join(entry['capabilities'])}")


if __name__ == "__main__":
    main()
