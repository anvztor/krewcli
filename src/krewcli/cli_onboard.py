"""Onboard CLI command — extracted from cli.py."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click
import uvicorn

from krewcli.agents.registry import AGENT_REGISTRY
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.gateway import _get_owner_label, _make_agent_id
from krewcli.presence.heartbeat import HeartbeatLoop

logger = logging.getLogger(__name__)


def register_onboard_command(main: click.Group) -> None:
    """Register the onboard command on the CLI group."""

    @main.command()
    @click.option("--cookbook", default=None, help="Cookbook ID (skips creation)")
    @click.option("--cookbook-name", default="my-cookbook", help="Name for new cookbook")
    @click.option("--owner", default="cli_user", help="Owner ID for cookbook creation")
    @click.option("--port", default=9999, type=int, help="A2A gateway port")
    @click.option("--workdir", default=None, help="Root working directory (default: ~/krew)")
    @click.option("--agents", default=None, help="Comma-separated agent types (skip selection)")
    @click.option("--max-concurrent", default=1, type=int, help="Max concurrent tasks per agent")
    @click.pass_context
    def onboard(ctx, cookbook, cookbook_name, owner, port, workdir, agents, max_concurrent):
        """Interactive onboarding — select recipes, agents, and launch gateway.

        \\b
        Steps:
          1. Create or select a cookbook
          2. Clone cookbook repo, select recipes (added as git submodules)
          3. Push submodules back (krewhub auto-indexes)
          4. Detect and select local agents
          5. Launch A2A gateway with per-recipe working directories

        \\b
        Examples:
          krewcli onboard
          krewcli onboard --cookbook-name my-project --owner drej
          krewcli onboard --cookbook CB_ID --agents claude,codex
        """
        import shutil

        settings = ctx.obj["settings"]
        settings = settings.model_copy(update={"agent_port": port})
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
    """Interactive onboarding: clone cookbook, select recipes + agents, launch gateway."""
    import shutil

    from krewcli.a2a.gateway_server import create_gateway_app
    from krewcli.cookbook_repo import (
        sanitize_name,
        add_recipe_submodule,
        clone_or_fetch,
        commit_and_push,
        configure_git_user,
        sync_submodules,
    )
    from krewcli.interactive import prompt_multi_select, prompt_single_select

    from krewcli.auth.token_store import load_token as _lt
    client = KrewHubClient(
        settings.krewhub_url, settings.api_key,
        jwt_token=_lt(), verify_ssl=settings.verify_ssl,
    )
    callback_url = f"{settings.krewhub_url}/api/v1/a2a/callback"

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

        # 3. Fetch available recipes
        cookbook_detail = await client.get_cookbook(cookbook_id)
        recipes = cookbook_detail.get("recipes", [])

        if not recipes:
            click.echo("\nNo recipes in this cookbook yet. Add recipes via krewhub first.")
            click.echo("Gateway will start, but no recipe-specific routing available.")
            recipe_contexts: dict[str, dict] = {}
        else:
            # 4. Interactive recipe selection
            recipe_items = [
                (r.get("name", r["id"]), r["id"])
                for r in recipes
            ]
            selected_indices = prompt_multi_select("Recipes", recipe_items)
            selected_recipes = [recipes[i] for i in selected_indices]

            click.echo(f"\nSelected {len(selected_recipes)} recipe(s)")

            # 5. Add selected recipes as submodules
            added_any = False
            for recipe in selected_recipes:
                name = recipe.get("name", recipe["id"])
                repo_url = recipe.get("repo_url", "")
                branch = recipe.get("default_branch", "main")

                if not repo_url:
                    click.echo(f"  Skipping {name}: no repo_url")
                    continue

                added = await add_recipe_submodule(
                    cookbook_dir, name, repo_url, branch=branch,
                )
                if added:
                    click.echo(f"  Added submodule: {name}")
                    added_any = True
                else:
                    click.echo(f"  Already present: {name}")

            # 6. Push submodules
            if added_any:
                pushed = await commit_and_push(
                    cookbook_dir, "onboard: add recipe submodules",
                )
                if pushed:
                    click.echo("  Pushed to krewhub (indexing triggered)")

            # 7. Sync submodules locally
            await sync_submodules(cookbook_dir)
            click.echo("  Submodules synced")

            # Build recipe_contexts
            recipe_contexts = {}
            for recipe in selected_recipes:
                name = recipe.get("name", recipe["id"])
                safe_name = sanitize_name(name)
                recipe_contexts[name] = {
                    "working_dir": os.path.join(cookbook_dir, safe_name),
                    "repo_url": recipe.get("repo_url", ""),
                    "branch": recipe.get("default_branch", "main"),
                }

        # 8. Detect and select agents
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

        # 9. Create gateway app
        app, spawn_manager, registered_agents = create_gateway_app(
            host=settings.agent_host,
            port=settings.agent_port,
            working_dir=cookbook_dir if recipe_contexts else working_dir,
            repo_url="",
            branch="main",
            callback_url=callback_url,
            api_key=settings.api_key,
            agent_names=resolved_agent_names,
            max_concurrent=max_concurrent,
            recipe_contexts=recipe_contexts,
            krewhub_client=client,
        )

        click.echo(f"\nGateway agents: {', '.join(registered_agents)}")
        for name in registered_agents:
            click.echo(f"  /agents/{name} -> {name} CLI")

        # 10. Register agents and start heartbeats
        heartbeats: list[HeartbeatLoop] = []
        _owner_label = _get_owner_label()
        for name in registered_agents:
            agent_id = _make_agent_id(name, _owner_label)
            endpoint_url = f"http://{settings.agent_host}:{settings.agent_port}/agents/{name}"

            entry = AGENT_REGISTRY.get(name, {})
            display_name = entry.get("display_name", name)
            capabilities = entry.get("capabilities", [])

            try:
                await client.register_agent(
                    agent_id=agent_id,
                    cookbook_id=cookbook_id,
                    display_name=display_name,
                    capabilities=capabilities,
                    max_concurrent_tasks=max_concurrent,
                    endpoint_url=endpoint_url,
                )
                click.echo(f"  Registered {display_name} ({agent_id})")
            except Exception:
                logger.warning("Registration failed for %s, continuing", name)

            hb = HeartbeatLoop(
                client=client,
                agent_id=agent_id,
                cookbook_id=cookbook_id,
                display_name=display_name,
                capabilities=capabilities,
                interval=settings.heartbeat_interval,
                endpoint_url=endpoint_url,
            )
            hb.start()
            heartbeats.append(hb)

        click.echo(f"\nOnboarding complete:")
        click.echo(f"  Cookbook: {cookbook_id}")
        click.echo(f"  Workspace: {cookbook_dir if recipe_contexts else working_dir}")
        if recipe_contexts:
            click.echo(f"  Recipes: {', '.join(recipe_contexts.keys())}")
        click.echo(f"  Agents: {', '.join(registered_agents)}")
        click.echo(f"  Gateway: http://{settings.agent_host}:{settings.agent_port}")
        click.echo(f"  KrewHub: {settings.krewhub_url}")
        click.echo(f"\nGateway ready. Waiting for tasks. Press Ctrl+C to stop.")

        # 11. Serve
        config = uvicorn.Config(
            app, host=settings.agent_host, port=settings.agent_port, log_level="info",
        )
        server = uvicorn.Server(config)

        loop = asyncio.get_running_loop()
        _orig_handler = loop.get_exception_handler()

        def _shutdown_exception_handler(loop, context):
            exc = context.get("exception")
            if isinstance(exc, (asyncio.InvalidStateError, OSError, BrokenPipeError)):
                return
            if _orig_handler:
                _orig_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        try:
            await server.serve()
        finally:
            loop.set_exception_handler(_shutdown_exception_handler)
            await spawn_manager.shutdown()
            for hb in heartbeats:
                try:
                    await hb.stop()
                except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
                    pass
            try:
                await client.close()
            except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
                pass
            click.echo("Gateway stopped. Goodbye.")

    except Exception as exc:
        try:
            await client.close()
        except Exception:
            pass
        if not isinstance(exc, SystemExit):
            click.echo(f"Onboard failed: {exc}")
            raise SystemExit(1)
        raise
