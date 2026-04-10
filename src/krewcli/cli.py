from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click
import httpx
import uvicorn

from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.config import get_settings
from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.repo_diagram import build_repo_diagram
from krewcli.workflow.digest_builder import DigestBuilder
from krewcli.workflow.task_runner import TaskRunner


@click.group()
@click.pass_context
def main(ctx: click.Context) -> None:
    """KrewCLI — bring your agents online on Cookrew."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    settings = get_settings()
    ctx.obj["settings"] = settings

    # Load JWT from ~/.krewcli/token if available (from 'krewcli login')
    from krewcli.auth.token_store import load_token
    jwt_token = load_token()

    ctx.obj["client"] = KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=jwt_token,
    )


# ── join: the universal agent onboarding command ──


@main.command()
@click.option("--recipe", required=True, help="Recipe ID to join")
@click.option("--cookbook", default=None, help="Cookbook ID (required for agent registration)")
@click.option("--port", default=9999, type=int, help="A2A server port")
@click.option("--agent-id", default=None, help="Override agent ID prefix")
@click.option("--workdir", default=".", help="Working directory for agent")
@click.option("--agents", default=None, help="Comma-separated list of agent types (auto-detect if omitted)")
@click.option("--max-concurrent", default=1, type=int, help="Max concurrent tasks per agent type")
# Legacy tier options (still supported for single-agent mode)
@click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default=None, help="Single CLI agent backend (legacy)")
@click.option("--provider", type=click.Choice(["anthropic", "openai"]), default=None, help="LLM provider for direct call")
@click.option("--model", default=None, help="Override model name")
@click.option("--endpoint", default=None, help="Remote A2A agent URL")
@click.option("--framework", type=click.Choice(["anthropic", "openai"]), default=None, help="pydantic-ai framework agent")
@click.option("--orchestrator", is_flag=True, default=False, help="Run as orchestrator")
@click.pass_context
def join(ctx, recipe, cookbook, port, agent_id, workdir, agents, max_concurrent, agent, provider, model, endpoint, framework, orchestrator):
    """Bring agents online as an A2A gateway.

    By default, auto-detects available CLIs (claude, codex, bub) on PATH
    and exposes each at /agents/{name} as a separate A2A endpoint.
    krewhub dispatches tasks directly to these endpoints via A2A.

    \b
    Gateway mode (default — multi-agent):
      krewcli join --recipe ID --cookbook CB
      krewcli join --recipe ID --agents claude,codex --max-concurrent 2

    \b
    Legacy single-agent modes (still supported):
      krewcli join --recipe ID --agent claude
      krewcli join --recipe ID --provider anthropic
      krewcli join --recipe ID --framework anthropic
      krewcli join --recipe ID --endpoint http://my-agent:8080
      krewcli join --recipe ID --orchestrator --provider anthropic
    """
    settings = ctx.obj["settings"]
    settings = settings.model_copy(update={"agent_port": port})
    resolved_workdir = os.path.abspath(workdir)

    # Detect if using legacy single-agent mode
    is_legacy = any([provider, model, endpoint, framework, orchestrator, (agent and not agents)])

    if is_legacy:
        mode, executor, card, display_name, caps = _resolve_mode(
            agent=agent, provider=provider, model=model, framework=framework,
            endpoint=endpoint, orchestrator=orchestrator,
            host=settings.agent_host, port=port, working_dir=resolved_workdir,
            settings=settings,
        )
        resolved_id = agent_id or f"{mode.replace(':', '_')}_{os.getpid()}"
        click.echo("Bringing agent online (legacy single-agent mode)")
        click.echo(f"  Mode: {mode}")
        click.echo(f"  Agent: {display_name} ({resolved_id})")
        click.echo(f"  Recipe: {recipe}")
        click.echo(f"  A2A: http://{settings.agent_host}:{port}")

        resolved_cookbook = cookbook or settings.default_cookbook_id
        if not resolved_cookbook:
            raise click.UsageError("Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID")

        asyncio.run(_run_agent(
            settings=settings, recipe_id=recipe, cookbook_id=resolved_cookbook,
            agent_id=resolved_id,
            display_name=display_name, capabilities=caps,
            executor=executor, card=card, working_dir=resolved_workdir,
            mode=mode, agent_name=agent,
        ))
        return

    # Gateway mode — multi-agent
    agent_names = agents.split(",") if agents else None
    resolved_cookbook = cookbook or settings.default_cookbook_id
    if not resolved_cookbook:
        raise click.UsageError("Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID")

    click.echo("Starting A2A gateway")
    click.echo(f"  Recipe: {recipe}")
    click.echo(f"  Cookbook: {resolved_cookbook}")
    click.echo(f"  Work dir: {resolved_workdir}")
    click.echo(f"  Port: {port}")
    click.echo(f"  Max concurrent per agent: {max_concurrent}")
    click.echo(f"  KrewHub: {settings.krewhub_url}")

    asyncio.run(_run_gateway(
        settings=settings,
        recipe_id=recipe,
        cookbook_id=resolved_cookbook,
        agent_id_prefix=agent_id or f"gw_{os.getpid()}",
        working_dir=resolved_workdir,
        agent_names=agent_names,
        max_concurrent=max_concurrent,
    ))


def _resolve_mode(agent, provider, model, framework, endpoint, orchestrator, host, port, working_dir, settings):
    if agent:
        from krewcli.a2a.executors.cli_agent import CLIExecutor, build_cli_agent_card
        executor = CLIExecutor(agent_name=agent, working_dir=working_dir)
        card = build_cli_agent_card(agent, host, port)
        info = get_agent_info(agent)
        return f"cli:{agent}", executor, card, info["display_name"], info["capabilities"]

    if provider and not orchestrator:
        from krewcli.a2a.executors.direct_llm import DirectLLMExecutor, build_direct_llm_card
        m = model or _default_model(provider)
        executor = DirectLLMExecutor(model=f"{provider}:{m}")
        card = build_direct_llm_card(provider, host, port)
        return f"llm:{provider}", executor, card, f"LLM ({provider})", ["summarize", "classify", "plan", "review"]

    if framework:
        from krewcli.a2a.executors.framework_agent import FrameworkExecutor, build_framework_card
        m = model or _default_model(framework)
        executor = FrameworkExecutor(model=f"{framework}:{m}", working_dir=working_dir)
        card = build_framework_card(framework, host, port)
        return f"framework:{framework}", executor, card, f"Framework ({framework})", ["code", "implement", "fix", "test"]

    if endpoint:
        from krewcli.a2a.executors.remote_agent import RemoteExecutor, build_remote_card
        executor = RemoteExecutor(remote_url=endpoint)
        card = build_remote_card(endpoint, host, port)
        return "remote", executor, card, f"Remote ({endpoint})", ["code"]

    if orchestrator:
        from krewcli.a2a.executors.orchestrator_agent import OrchestratorExecutor, build_orchestrator_card
        krewhub_client = KrewHubClient(settings.krewhub_url, settings.api_key)
        cookbook = settings.default_cookbook_id
        executor = OrchestratorExecutor(
            krewhub_client=krewhub_client,
            cookbook_id=cookbook,
        )
        card = build_orchestrator_card(host, port)
        return "orchestrator", executor, card, "Orchestrator", ["orchestrate", "plan", "decompose", "coordinate"]

    raise click.UsageError("Specify one of: --agent, --provider, --framework, --endpoint, or --orchestrator")


def _default_model(provider):
    return {"anthropic": "claude-sonnet-4-20250514", "openai": "gpt-4o"}.get(provider, "claude-sonnet-4-20250514")


async def _run_agent(settings, recipe_id, cookbook_id, agent_id, display_name, capabilities, executor, card, working_dir, mode, agent_name=None):
    from krewcli.a2a.server import create_a2a_app
    client = KrewHubClient(settings.krewhub_url, settings.api_key)

    endpoint_url = f"http://{settings.agent_host}:{settings.agent_port}"

    try:
        await client.register_agent(
            agent_id=agent_id, cookbook_id=cookbook_id,
            display_name=display_name, capabilities=capabilities,
            endpoint_url=endpoint_url,
        )
    except Exception:
        logging.getLogger(__name__).warning("Registration failed, continuing with heartbeat")

    heartbeat = HeartbeatLoop(
        client=client, agent_id=agent_id, cookbook_id=cookbook_id,
        display_name=display_name, capabilities=capabilities,
        interval=settings.heartbeat_interval,
        endpoint_url=endpoint_url,
    )
    heartbeat.start()

    auth_service = _build_auth_service(settings)

    app = create_a2a_app(agent_card=card, executor=executor, auth_service=auth_service)
    config = uvicorn.Config(app, host=settings.agent_host, port=settings.agent_port, log_level="info")
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
        try:
            await heartbeat.stop()
        except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
            pass
        try:
            await client.close()
        except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
            pass


async def _run_gateway(
    settings, recipe_id, cookbook_id, agent_id_prefix, working_dir,
    agent_names, max_concurrent,
):
    """Run the multi-agent A2A gateway."""
    import shutil
    import click as _click

    from krewcli.a2a.gateway_server import create_gateway_app

    from krewcli.auth.token_store import load_token as _lt
    client = KrewHubClient(settings.krewhub_url, settings.api_key, jwt_token=_lt())
    callback_url = f"{settings.krewhub_url}/api/v1/a2a/callback"

    repo_url, branch = await _load_recipe_context(client, recipe_id)

    app, spawn_manager, registered_agents = create_gateway_app(
        host=settings.agent_host,
        port=settings.agent_port,
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        callback_url=callback_url,
        api_key=settings.api_key,
        agent_names=agent_names,
        max_concurrent=max_concurrent,
        krewhub_client=client,
    )

    _click.echo(f"  Agents: {', '.join(registered_agents)}")
    for name in registered_agents:
        _click.echo(f"    /agents/{name} -> {name} CLI")

    # --- ERC-4337: session key + smart account ---
    from krewcli.auth.token_store import load_token as _load_token
    from krewcli.session_key import load_session_key, get_session_key_address

    _jwt = _load_token()
    erc8004_ids: dict[str, int] = {}
    session_addr = get_session_key_address()

    if _jwt and session_addr:
        auth_url = settings.krew_auth_url

        # Get smart account info
        try:
            acct_resp = await asyncio.to_thread(
                lambda: httpx.get(f"{auth_url}/auth/account/info", params={"token": _jwt}, timeout=10).json()
            )
            smart_addr = acct_resp.get("smart_address")
            _click.echo(f"\n  Smart Account: {smart_addr}")
            _click.echo(f"  Session Key: {session_addr}")

            if smart_addr:
                # Request session key approval for each agent
                for name in registered_agents:
                    display_name, capabilities = _gateway_agent_metadata(name)
                    try:
                        req_resp = await asyncio.to_thread(lambda n=name, dn=display_name: httpx.post(
                            f"{auth_url}/auth/session-keys/request",
                            json={
                                "token": _jwt,
                                "agent_name": n,
                                "session_pubkey": session_addr,
                                "allowed_targets": [settings.erc8004_identity_registry],
                                "allowed_selectors": ["0xf2c298be"],  # register(string)
                                "spend_limit": "0",
                                "valid_hours": 24,
                            },
                            timeout=10,
                        ).json())
                        _click.echo(f"  Session key requested for {dn}: {req_resp.get('status', 'unknown')}")
                    except Exception as e:
                        _click.echo(f"  Session key request failed for {name}: {e}")

                _click.echo(f"\n  Approve session keys in cookrew to enable on-chain operations.")
                _click.echo(f"  Off-chain operations (task claims, events) work immediately via JWT.")
            else:
                _click.echo(f"  No smart account — connect wallet in cookrew first")
        except Exception as e:
            _click.echo(f"\n  Account lookup failed: {e}")
    elif _jwt:
        _click.echo("\n  No session key — run 'krewcli session-key create' first")
    else:
        _click.echo("\n  No session — run 'krewcli login' first")

    # Register each agent type in krewhub
    heartbeats: list[HeartbeatLoop] = []
    for name in registered_agents:
        agent_id = f"{name}@{settings.agent_host}:{settings.agent_port}"
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
            erc_tag = f" (ERC-8004 #{erc8004_ids[name]})" if name in erc8004_ids else ""
            _click.echo(f"  Registered {display_name} ({agent_id}){erc_tag}")
        except Exception:
            logging.getLogger(__name__).warning(
                "Registration failed for %s, continuing with heartbeat", name
            )

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

    _click.echo(f"\nGateway ready. Waiting for tasks from krewhub.")

    config = uvicorn.Config(
        app, host=settings.agent_host, port=settings.agent_port, log_level="info"
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


def _build_auth_service(settings):
    """Build auth service if JWT secret is configured."""
    if not settings.jwt_secret:
        logging.getLogger(__name__).warning(
            "KREWCLI_JWT_SECRET is not set — auth is DISABLED"
        )
        return None
    if len(settings.jwt_secret) < 32:
        logging.getLogger(__name__).warning(
            "KREWCLI_JWT_SECRET is set but shorter than 32 chars — auth disabled"
        )
        return None
    from krewcli.auth.service import AuthService
    logging.getLogger(__name__).info("Auth enabled (JWT middleware active)")
    return AuthService(
        jwt_secret=settings.jwt_secret,
        token_expiry_minutes=settings.token_expiry_minutes,
    )


# ── claim: one-shot task execution ──


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
    resolved_id = agent_id or f"{agent}_{os.getpid()}"
    info = get_agent_info(agent)

    async def _run():
        repo_url, branch = await _load_recipe_context(client, recipe)
        heartbeat = HeartbeatLoop(
            client=client, agent_id=resolved_id, recipe_id=recipe,
            display_name=info["display_name"], capabilities=info["capabilities"],
            interval=settings.heartbeat_interval,
        )
        heartbeat.start()
        runner = TaskRunner(
            client=client, heartbeat=heartbeat, agent_name=agent,
            agent_id=resolved_id, working_dir=os.path.abspath(workdir),
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


# ── onboard: interactive workspace + gateway ──


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

    \b
    Steps:
      1. Create or select a cookbook
      2. Clone cookbook repo, select recipes (added as git submodules)
      3. Push submodules back (krewhub auto-indexes)
      4. Detect and select local agents
      5. Launch A2A gateway with per-recipe working directories

    \b
    Examples:
      krewcli onboard
      krewcli onboard --cookbook-name my-project --owner drej
      krewcli onboard --cookbook CB_ID --agents claude,codex
    """
    import shutil

    settings = ctx.obj["settings"]
    settings = settings.model_copy(update={"agent_port": port})
    resolved_workdir = workdir or os.path.join(Path.home(), "krew")

    # Pre-filter agents list if provided
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
    client = KrewHubClient(settings.krewhub_url, settings.api_key, jwt_token=_lt())
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

            # 6. Push submodules (triggers krewhub post-receive indexing)
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
        for name in registered_agents:
            agent_id = f"{name}@{settings.agent_host}:{settings.agent_port}"
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
                logging.getLogger(__name__).warning(
                    "Registration failed for %s, continuing", name,
                )

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


# ── Legacy start command ──


@main.command()
@click.option("--recipe", required=True)
@click.option("--cookbook", default=None)
@click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default="claude")
@click.option("--agent-id", default=None)
@click.option("--port", default=9999, type=int)
@click.option("--workdir", default=".")
@click.option("--mode", type=click.Choice(["poll", "watch"]), default="poll", hidden=True)
@click.pass_context
def start(ctx, recipe, cookbook, agent, agent_id, port, workdir, mode):
    """[Legacy] Start an agent. Use 'join' or 'onboard' instead."""
    ctx.invoke(join, recipe=recipe, cookbook=cookbook, agent=agent, agent_id=agent_id, port=port, workdir=workdir)


# ── Task worker ──


async def _run_task_worker(settings, client, heartbeat, recipe_id, agent_name, agent_id, working_dir):
    repo_url, branch = await _load_recipe_context(client, recipe_id)
    runner = TaskRunner(client=client, heartbeat=heartbeat, agent_name=agent_name, agent_id=agent_id, working_dir=working_dir, repo_url=repo_url, branch=branch)
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


async def _load_recipe_context(client, recipe_id):
    detail = await client.get_recipe(recipe_id)
    r = detail.get("recipe", {})
    return r.get("repo_url", ""), r.get("default_branch", "main")


# ── Auth commands ──


@main.group("wallet")
def wallet_group():
    """Manage wallet identity for SIWE authentication."""
    pass


@wallet_group.command("create")
def wallet_create():
    """Generate a new Ethereum wallet and save to ~/.krewcli/wallet."""
    from krewcli.auth.wallet import generate_wallet

    address, key_hex = generate_wallet()
    click.echo(f"Wallet created: {address}")
    click.echo(f"Private key saved to ~/.krewcli/wallet")
    click.echo(f"Back up this key! If lost, you lose access to this identity.")


@wallet_group.command("import")
@click.argument("private_key")
def wallet_import(private_key):
    """Import an existing private key. Usage: krewcli wallet import 0x..."""
    from eth_account import Account
    from krewcli.auth.wallet import save_private_key

    try:
        acct = Account.from_key(private_key)
    except Exception:
        click.echo("Error: Invalid private key.", err=True)
        raise SystemExit(1)

    save_private_key(private_key)
    click.echo(f"Wallet imported: {acct.address}")
    click.echo(f"Saved to ~/.krewcli/wallet")


@wallet_group.command("address")
def wallet_address():
    """Show the current wallet address."""
    from krewcli.auth.wallet import get_wallet_address

    addr = get_wallet_address()
    if addr is None:
        click.echo("No wallet found. Run 'krewcli wallet create' first.", err=True)
        raise SystemExit(1)
    click.echo(addr)


@main.group("session-key")
def session_key_group():
    """Manage session keys for ERC-4337 smart account operations."""
    pass


@session_key_group.command("create")
def session_key_create():
    """Generate a new session key for agent operations."""
    from krewcli.session_key import generate_session_key

    address, _ = generate_session_key()
    click.echo(f"Session key created: {address}")
    click.echo("Saved to ~/.krewcli/session_key")
    click.echo("Request approval: human must call addSessionKey() on the smart account")


@session_key_group.command("address")
def session_key_address():
    """Show the current session key address."""
    from krewcli.session_key import get_session_key_address

    addr = get_session_key_address()
    if addr is None:
        click.echo("No session key. Run 'krewcli session-key create'.", err=True)
        raise SystemExit(1)
    click.echo(addr)


@main.command("login")
@click.pass_context
def login(ctx):
    """Log in via krewauth device authorization (approve in browser).

    Opens the krewauth login page. Authenticate with passkey or wallet.
    No private key needed on this machine.
    """
    import time
    from krewcli.auth.token_store import save_token

    settings = ctx.obj["settings"]
    auth_url = settings.krew_auth_url

    try:
        with httpx.Client(timeout=10) as http:
            # 1. Request a device code from krewauth
            resp = http.post(f"{auth_url}/auth/device/request")
            resp.raise_for_status()
            data = resp.json()
            device_code = data["device_code"]
            user_code = data["user_code"]
            # Always use the client-side auth URL (not server's self-reported URL)
            verification_uri = f"{auth_url}/auth/login?device_code={user_code}"
            expires_in = data["expires_in"]

            click.echo()
            click.echo(f"  Open: {verification_uri}")
            click.echo(f"  Code: {user_code}")
            click.echo()
            click.echo(f"  Waiting for approval (expires in {expires_in // 60} min)...")

            # Try to open browser automatically
            import webbrowser
            webbrowser.open(verification_uri)

            # 2. Poll until approved
            poll_interval = 3
            elapsed = 0
            while elapsed < expires_in:
                time.sleep(poll_interval)
                elapsed += poll_interval

                resp = http.post(
                    f"{auth_url}/auth/device/token",
                    json={"device_code": device_code},
                )
                if resp.status_code == 404:
                    click.echo("Error: Code expired.", err=True)
                    raise SystemExit(1)

                resp.raise_for_status()
                result = resp.json()

                if result["status"] == "approved":
                    save_token(result["token"])
                    click.echo(f"\n  Logged in as {result.get('account_id', 'unknown')}")
                    if result.get("wallet_address"):
                        click.echo(f"  Wallet: {result['wallet_address']}")
                    click.echo(f"  Session expires: {result['expires_at']}")
                    click.echo(f"  JWT saved to ~/.krewcli/token")
                    return

            click.echo("Error: Timed out waiting for approval.", err=True)
            raise SystemExit(1)

    except httpx.ConnectError:
        click.echo(f"Error: Could not connect to krewauth at {auth_url}", err=True)
        raise SystemExit(1)


# ── Utility commands ──


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


@main.command()
@click.pass_context
def status(ctx):
    """Show available agent backends."""
    for name, entry in AGENT_REGISTRY.items():
        click.echo(f"  {name}: {entry['display_name']}")
        click.echo(f"    capabilities: {', '.join(entry['capabilities'])}")


@main.command("repo-diagram")
@click.option("--root", default=".", show_default=True, type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.option("--format", "diagram_format", default="mermaid", show_default=True, type=click.Choice(["mermaid", "tree"]))
@click.option("--max-depth", default=3, show_default=True, type=click.IntRange(min=0))
@click.option("--include-hidden", is_flag=True, default=False, help="Include hidden files and directories.")
def repo_diagram(root: Path, diagram_format: str, max_depth: int, include_hidden: bool) -> None:
    """Render a repository structure diagram."""
    click.echo(
        build_repo_diagram(
            root=root,
            format=diagram_format,
            max_depth=max_depth,
            include_hidden=include_hidden,
        )
    )


if __name__ == "__main__":
    main()
