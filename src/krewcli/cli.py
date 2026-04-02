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
@click.option(
    "--mode",
    type=click.Choice(["poll", "watch"]),
    default="poll",
    help="Task delivery mode: poll (legacy) or watch (scheduler-assigned)",
)
@click.pass_context
def start(
    ctx: click.Context,
    recipe: str,
    agent: str,
    agent_id: str | None,
    workdir: str,
    mode: str,
) -> None:
    """Start the A2A agent server with heartbeat to KrewHub."""
    settings = ctx.obj["settings"]
    info = get_agent_info(agent)
    resolved_agent_id = agent_id or f"{agent}_{os.getpid()}"
    resolved_workdir = os.path.abspath(workdir)

    click.echo("Starting KrewCLI agent server")
    click.echo(f"  Agent: {info['display_name']} ({resolved_agent_id})")
    click.echo(f"  Recipe: {recipe}")
    click.echo(f"  Work dir: {resolved_workdir}")
    click.echo(f"  Mode: {mode}")
    click.echo(f"  A2A endpoint: http://{settings.agent_host}:{settings.agent_port}")
    click.echo(f"  KrewHub: {settings.krewhub_url}")

    asyncio.run(_run_server(
        settings=settings,
        recipe_id=recipe,
        agent_name=agent,
        agent_id=resolved_agent_id,
        working_dir=resolved_workdir,
        mode=mode,
    ))


async def _run_server(
    settings,
    recipe_id: str,
    agent_name: str,
    agent_id: str,
    working_dir: str,
    mode: str = "poll",
) -> None:
    from krewcli.a2a.server import create_a2a_app

    client = KrewHubClient(settings.krewhub_url, settings.api_key)
    repo_url, branch = await _load_recipe_context(client, recipe_id)

    node_agent = None
    worker_task = None

    if mode == "watch":
        from krewcli.node.agent import NodeAgent
        node_agent = NodeAgent(
            client=client,
            agent_name=agent_name,
            agent_id=agent_id,
            recipe_id=recipe_id,
            working_dir=working_dir,
            repo_url=repo_url,
            branch=branch,
            heartbeat_interval=settings.heartbeat_interval,
            krewhub_url=settings.krewhub_url,
            api_key=settings.api_key,
        )
        await node_agent.start()
    else:
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

        worker_task = asyncio.create_task(
            _run_task_worker(
                settings=settings,
                client=client,
                heartbeat=heartbeat,
                recipe_id=recipe_id,
                agent_name=agent_name,
                agent_id=agent_id,
                working_dir=working_dir,
            )
        )

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
        if node_agent is not None:
            await node_agent.stop()
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await heartbeat.stop()
        await client.close()


async def _run_task_worker(
    settings,
    client: KrewHubClient,
    heartbeat: HeartbeatLoop,
    recipe_id: str,
    agent_name: str,
    agent_id: str,
    working_dir: str,
) -> None:
    repo_url, branch = await _load_recipe_context(client, recipe_id)
    runner = TaskRunner(
        client=client,
        heartbeat=heartbeat,
        agent_name=agent_name,
        agent_id=agent_id,
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
    )
    digest_builders: dict[str, DigestBuilder] = {}

    while True:
        try:
            await _run_task_worker_once(
                client=client,
                runner=runner,
                heartbeat=heartbeat,
                recipe_id=recipe_id,
                agent_id=agent_id,
                digest_builders=digest_builders,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Task worker cycle failed")

        await asyncio.sleep(settings.task_poll_interval)


async def _run_task_worker_once(
    client: KrewHubClient,
    runner: TaskRunner,
    heartbeat: HeartbeatLoop,
    recipe_id: str,
    agent_id: str,
    digest_builders: dict[str, DigestBuilder],
) -> bool:
    if heartbeat.current_task_id is not None:
        return False

    tasks = await client.list_tasks(recipe_id)
    open_tasks = [task for task in tasks if task.get("status") == "open"]
    if not open_tasks:
        return False

    task = open_tasks[0]
    result = await runner.claim_and_execute(task["id"])
    if result is None or not result.success:
        return True

    bundle_id = task["bundle_id"]
    builder = digest_builders.setdefault(bundle_id, DigestBuilder(client=client, agent_id=agent_id))
    builder.add_result(task["id"], result)

    bundle = await client.get_bundle(bundle_id)
    bundle_data = bundle.get("bundle", {})
    bundle_tasks = bundle.get("tasks", [])

    if bundle_data.get("status") == "cooked":
        task_ids = [item["id"] for item in bundle_tasks]
        if builder.has_results_for_tasks(task_ids):
            digest = await builder.submit(bundle_id)
            if digest is not None:
                digest_builders.pop(bundle_id, None)

    return True


async def _load_recipe_context(
    client: KrewHubClient,
    recipe_id: str,
) -> tuple[str, str]:
    recipe_detail = await client.get_recipe(recipe_id)
    recipe = recipe_detail.get("recipe", {})
    return (
        recipe.get("repo_url", ""),
        recipe.get("default_branch", "main"),
    )


@main.command("list-tasks")
@click.option("--recipe", required=True, help="Recipe ID")
@click.pass_context
def list_tasks(ctx: click.Context, recipe: str) -> None:
    """List available tasks for a recipe."""
    client: KrewHubClient = ctx.obj["client"]

    async def _run():
        try:
            tasks = await client.list_tasks(recipe, bundle_statuses=("open", "claimed"))
            seen_bundles: set[str] = set()

            for task in tasks:
                bundle_id = task["bundle_id"]
                if bundle_id not in seen_bundles:
                    seen_bundles.add(bundle_id)
                    click.echo(f"\nBundle: {bundle_id} [{task['bundle_status']}]")
                    click.echo(f"  Prompt: {task['bundle_prompt'][:80]}")

                status_icon = {
                    "open": "[ ]",
                    "claimed": "[>]",
                    "working": "[~]",
                    "done": "[x]",
                    "blocked": "[!]",
                    "cancelled": "[-]",
                }.get(task["status"], "[?]")
                agent = task.get("claimed_by_agent_id", "")
                agent_str = f" ({agent})" if agent else ""
                click.echo(f"    {status_icon} {task['id']}: {task['title']}{agent_str}")
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
        repo_url, branch = await _load_recipe_context(client, recipe)
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
            repo_url=repo_url,
            branch=branch,
        )

        try:
            result = await runner.claim_and_execute(task_id)
            if result is None:
                click.echo(f"Task {task_id} failed or could not be claimed")
            elif result.success:
                click.echo(f"Task {task_id} completed: {result.summary}")
            else:
                reason = result.blocked_reason or result.summary
                click.echo(f"Task {task_id} blocked: {reason}")
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
