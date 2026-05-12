"""Claim command — one-shot task execution via the daemon harness."""

from __future__ import annotations

import asyncio
import os

import click

from krewcli.backend.registry import BACKEND_INFO, get_backend


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


def register_claim_commands(main: click.Group) -> None:
    """Register the claim command on the CLI group."""

    @main.command()
    @click.argument("task_id")
    @click.option("--cookbook", required=True)
    @click.option("--agent", type=click.Choice(list(BACKEND_INFO.keys())), default="claude")
    @click.option("--agent-id", default=None)
    @click.option("--workdir", default=".")
    @click.pass_context
    def claim(ctx, task_id, cookbook, agent, agent_id, workdir):
        """Claim and execute a single task."""
        client = ctx.obj["client"]
        settings = ctx.obj["settings"]
        os_module = _compat_lookup("os", os)
        resolved_workdir = os_module.path.abspath(workdir)
        resolved_id = agent_id or f"{agent}_{os_module.getpid()}"
        info = BACKEND_INFO.get(agent, {})

        async def _run():
            from krewcli.daemon.harness import Harness
            from krewcli.daemon.session import Session
            from krewcli.daemon.execenv import ExecutionEnvironment
            from krewcli.gateway.identity import _get_owner_label, _make_agent_id
            from krewcli.presence.heartbeat import HeartbeatLoop

            owner = _get_owner_label()
            agent_id_full = _make_agent_id(agent, owner)

            # Register + heartbeat
            try:
                await client.register_agent(
                    agent_id=agent_id_full,
                    cookbook_id=cookbook,
                    display_name=info.get("display_name", agent),
                    capabilities=info.get("capabilities", ["claim"]),
                )
            except Exception:
                pass

            heartbeat = HeartbeatLoop(
                client=client, agent_id=agent_id_full, cookbook_id=cookbook,
                display_name=info.get("display_name", agent),
                capabilities=info.get("capabilities", ["claim"]),
                interval=settings.heartbeat_interval,
            )
            heartbeat.start()

            # Claim
            try:
                claimed = await client.claim_task(task_id, agent_id_full)
            except Exception as exc:
                click.echo(f"Failed to claim task {task_id}: {exc}")
                await heartbeat.stop()
                await client.close()
                return

            # Execute via harness. Repo binding is now per-bundle
            # (bundle.repo_spec), not per-recipe — claim doesn't need
            # to resolve it up-front.
            backend = get_backend(agent)
            session = Session(client, task_id, agent_id_full)
            execenv = ExecutionEnvironment(
                base_dir=resolved_workdir,
                task_id=task_id,
                bundle_id=claimed.get("bundle_id", ""),
                repo_url="",
                branch="",
            )
            prompt = f"# Task: {claimed.get('title', '')}\n\n{claimed.get('description', '')}"

            harness = Harness(client)
            try:
                result = await harness.execute(
                    backend=backend,
                    session=session,
                    execenv=execenv,
                    prompt=prompt,
                    task_id=task_id,
                    task_title=claimed.get("title", ""),
                    task_description=claimed.get("description", ""),
                    cookbook_id=cookbook,
                    bundle_id=claimed.get("bundle_id", ""),
                )
                if result.success:
                    click.echo(f"Task {task_id} completed: {result.summary[:120]}")
                else:
                    click.echo(f"Task {task_id} blocked: {result.summary[:120]}")
            finally:
                await heartbeat.stop()
                await client.close()

        asyncio.run(_run())
