"""Task-related commands — list-tasks, milestone."""

from __future__ import annotations

import asyncio

import click

from krewcli.recipe_context import load_recipe_context


def _compat_lookup(name: str, default):
    import krewcli.cli as cli
    value = getattr(cli, name, default)
    return default if value is default else value


async def _load_recipe_context(client, recipe_id):
    return await load_recipe_context(client, recipe_id)


async def _run_task_worker(*_args, **_kwargs):
    """Removed — use 'krewcli daemon start' instead."""
    raise NotImplementedError(
        "Legacy task worker removed. Use 'krewcli daemon start' for continuous task execution."
    )


async def _run_task_worker_once(*_args, **_kwargs):
    """Removed — use 'krewcli daemon start' instead."""
    raise NotImplementedError("Legacy task worker removed.")


def register_task_commands(main: click.Group) -> None:
    """Register list-tasks and milestone commands on the CLI group."""

    @main.command("list-tasks")
    @click.option("--recipe", required=True)
    @click.pass_context
    def list_tasks(ctx, recipe):
        """List available tasks for a recipe."""
        client = ctx.obj["client"]

        async def _run():
            try:
                tasks = await client.list_tasks(recipe, bundle_statuses=("open", "claimed"))
                seen = set()
                for task in tasks:
                    bid = task["bundle_id"]
                    if bid not in seen:
                        seen.add(bid)
                        click.echo(f"\nBundle: {bid} [{task['bundle_status']}]")
                        click.echo(f"  Prompt: {task['bundle_prompt'][:80]}")
                    si = {
                        "open": "[ ]", "claimed": "[>]", "working": "[~]",
                        "done": "[x]", "blocked": "[!]", "cancelled": "[-]",
                    }.get(task["status"], "[?]")
                    ag = task.get("claimed_by_agent_id", "")
                    click.echo(f"    {si} {task['id']}: {task['title']}{f' ({ag})' if ag else ''}")
            finally:
                await client.close()

        asyncio.run(_run())

    @main.command()
    @click.argument("task_id")
    @click.option("--body", required=True)
    @click.option("--fact", multiple=True)
    @click.option("--agent-id", default="cli_user")
    @click.pass_context
    def milestone(ctx, task_id, body, fact, agent_id):
        """Post a milestone event to a task."""
        client = ctx.obj["client"]

        async def _run():
            try:
                facts = [{"id": f"f_{i}", "claim": f, "captured_by": agent_id} for i, f in enumerate(fact)]
                event = await client.post_event(
                    task_id=task_id, event_type="milestone",
                    actor_id=agent_id, body=body, facts=facts,
                )
                click.echo(f"Milestone posted: {event['id']}")
            finally:
                await client.close()

        asyncio.run(_run())
