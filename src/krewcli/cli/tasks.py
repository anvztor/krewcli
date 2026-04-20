"""Task-related commands and helpers — list-tasks, milestone, task worker."""

from __future__ import annotations

import asyncio
import logging

import click

from krewcli.gateway_helpers import load_recipe_context
from krewcli.workflow.digest_builder import DigestBuilder
from krewcli.workflow.task_runner import TaskRunner


def _compat_lookup(name: str, default):
    import krewcli.cli as cli

    value = getattr(cli, name, default)
    return default if value is default else value


async def _load_recipe_context(client, recipe_id):
    return await load_recipe_context(client, recipe_id)


async def _run_task_worker(settings, client, heartbeat, recipe_id, agent_name, agent_id, working_dir):
    recipe_context_loader = _compat_lookup("_load_recipe_context", _load_recipe_context)
    runner_cls = _compat_lookup("TaskRunner", TaskRunner)
    repo_url, branch = await recipe_context_loader(client, recipe_id)
    runner = runner_cls(
        client=client, heartbeat=heartbeat, agent_name=agent_name,
        agent_id=agent_id, working_dir=working_dir,
        repo_url=repo_url, branch=branch,
    )
    digest_builders: dict[str, DigestBuilder] = {}
    while True:
        try:
            await _run_task_worker_once(client, runner, heartbeat, recipe_id, agent_id, digest_builders)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Task worker cycle failed")
        await asyncio.sleep(settings.task_poll_interval)


async def _run_task_worker_once(client, runner, heartbeat, recipe_id, agent_id, digest_builders):
    if heartbeat.current_task_id is not None:
        return False
    tasks = await client.list_tasks(recipe_id)
    open_tasks = [t for t in tasks if t.get("status") == "open"]
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
    if bundle.get("bundle", {}).get("status") == "cooked":
        task_ids = [item["id"] for item in bundle.get("tasks", [])]
        if builder.has_results_for_tasks(task_ids):
            digest = await builder.submit(bundle_id)
            if digest:
                digest_builders.pop(bundle_id, None)
    return True


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
                    si = {"open": "[ ]", "claimed": "[>]", "working": "[~]", "done": "[x]", "blocked": "[!]", "cancelled": "[-]"}.get(task["status"], "[?]")
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
                event = await client.post_event(task_id=task_id, event_type="milestone", actor_id=agent_id, body=body, facts=facts)
                click.echo(f"Milestone posted: {event['id']}")
            finally:
                await client.close()

        asyncio.run(_run())
