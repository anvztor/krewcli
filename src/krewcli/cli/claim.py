"""Claim command — one-shot task execution."""

from __future__ import annotations

import asyncio
import os

import click

from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.gateway_helpers import load_recipe_context
from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.workflow.task_runner import TaskRunner


def _compat_lookup(name: str, default):
    import krewcli.cli as cli

    command = getattr(cli, "claim", None)
    package_value = getattr(cli, name, default)
    command_value = getattr(command, name, default) if command is not None else default

    if command_value is not default and package_value is default:
        return command_value
    if package_value is not default and command_value is default:
        return package_value
    if command_value is not default and command_value is not package_value:
        return command_value
    if package_value is not default:
        return package_value
    return default


async def _load_recipe_context(client, recipe_id):
    return await load_recipe_context(client, recipe_id)


def register_claim_commands(main: click.Group) -> None:
    """Register the claim command on the CLI group."""

    @main.command()
    @click.argument("task_id")
    @click.option("--recipe", required=True)
    @click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default="claude")
    @click.option("--agent-id", default=None)
    @click.option("--workdir", default=".")
    @click.pass_context
    def claim(ctx, task_id, recipe, agent, agent_id, workdir):
        """Claim and execute a single task."""
        client = ctx.obj["client"]
        settings = ctx.obj["settings"]
        os_module = _compat_lookup("os", os)
        recipe_context_loader = _compat_lookup("_load_recipe_context", _load_recipe_context)
        heartbeat_cls = _compat_lookup("HeartbeatLoop", HeartbeatLoop)
        runner_cls = _compat_lookup("TaskRunner", TaskRunner)
        resolved_id = agent_id or f"{agent}_{os_module.getpid()}"
        info = get_agent_info(agent)

        async def _run():
            repo_url, branch = await recipe_context_loader(client, recipe)
            heartbeat = heartbeat_cls(
                client=client, agent_id=resolved_id, cookbook_id=recipe,
                display_name=info["display_name"], capabilities=info["capabilities"],
                interval=settings.heartbeat_interval,
            )
            heartbeat.start()
            runner = runner_cls(
                client=client, heartbeat=heartbeat, agent_name=agent,
                agent_id=resolved_id, working_dir=os_module.path.abspath(workdir),
                repo_url=repo_url, branch=branch,
            )
            try:
                result = await runner.claim_and_execute(task_id)
                if result is None:
                    click.echo(f"Task {task_id} failed or could not be claimed")
                elif result.success:
                    click.echo(f"Task {task_id} completed: {result.summary}")
                else:
                    click.echo(f"Task {task_id} blocked: {result.blocked_reason or result.summary}")
            finally:
                await heartbeat.stop()
                await client.close()

        asyncio.run(_run())
