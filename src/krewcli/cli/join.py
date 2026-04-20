"""Join and start commands — multi-agent gateway and legacy single-agent mode."""

from __future__ import annotations

import asyncio
import logging
import os

import click
import uvicorn

from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.gateway_helpers import build_auth_service as _build_auth_service
from krewcli.gateway_runtime import run_gateway as _run_gateway_impl
from krewcli.presence.heartbeat import HeartbeatLoop


def _compat_lookup(name: str, default):
    import krewcli.cli as cli

    command = getattr(cli, "join", None)
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


def _default_model(provider: str) -> str:
    return {"anthropic": "claude-sonnet-4-20250514", "openai": "gpt-4o"}.get(
        provider, "claude-sonnet-4-20250514"
    )


def _resolve_mode(
    agent, provider, model, framework, endpoint, orchestrator,
    host, port, working_dir, settings,
):
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
        from krewcli.a2a.executors.planner_agent import (
            PlannerOrchestratorExecutor,
            build_planner_card,
        )
        client_cls = _compat_lookup("KrewHubClient", KrewHubClient)
        krewhub_client = client_cls(
            settings.krewhub_url,
            settings.api_key,
            verify_ssl=settings.verify_ssl,
        )
        cookbook = settings.default_cookbook_id
        executor = PlannerOrchestratorExecutor(
            krewhub_client=krewhub_client,
            cookbook_id=cookbook,
        )
        card = build_planner_card(host, port)
        return "orchestrator", executor, card, "Planner", ["generate-graph"]

    raise click.UsageError("Specify one of: --agent, --provider, --framework, --endpoint, or --orchestrator")


async def _run_agent(
    settings, recipe_id, cookbook_id, agent_id, display_name, capabilities,
    executor, card, working_dir, mode, agent_name=None,
):
    from krewcli.a2a.server import create_a2a_app
    client_cls = _compat_lookup("KrewHubClient", KrewHubClient)
    heartbeat_cls = _compat_lookup("HeartbeatLoop", HeartbeatLoop)
    client = client_cls(
        settings.krewhub_url,
        settings.api_key,
        verify_ssl=settings.verify_ssl,
    )

    endpoint_url = f"http://{settings.agent_host}:{settings.agent_port}"

    try:
        await client.register_agent(
            agent_id=agent_id, cookbook_id=cookbook_id,
            display_name=display_name, capabilities=capabilities,
            endpoint_url=endpoint_url,
        )
    except Exception:
        logging.getLogger(__name__).warning("Registration failed, continuing with heartbeat")

    heartbeat = heartbeat_cls(
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
    """Delegate to gateway module."""
    await _run_gateway_impl(
        settings, recipe_id, cookbook_id, agent_id_prefix, working_dir,
        agent_names, max_concurrent,
    )


def register_join_commands(main: click.Group) -> None:
    """Register join and start commands on the CLI group."""

    @main.command()
    @click.option("--recipe", default=None, help="Recipe ID to join (interactive if omitted)")
    @click.option("--cookbook", default=None, help="Cookbook ID (interactive if omitted)")
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
        os_module = _compat_lookup("os", os)
        resolve_mode = _compat_lookup("_resolve_mode", _resolve_mode)
        run_agent = _compat_lookup("_run_agent", _run_agent)
        run_gateway = _compat_lookup("_run_gateway", _run_gateway)
        client_cls = _compat_lookup("KrewHubClient", KrewHubClient)
        resolved_workdir = os_module.path.abspath(workdir)

        # Detect if using legacy single-agent mode
        is_legacy = any([provider, model, endpoint, framework, orchestrator, (agent and not agents)])

        if is_legacy:
            mode, executor, card, display_name, caps = resolve_mode(
                agent=agent, provider=provider, model=model, framework=framework,
                endpoint=endpoint, orchestrator=orchestrator,
                host=settings.agent_host, port=port, working_dir=resolved_workdir,
                settings=settings,
            )
            resolved_id = agent_id or f"{mode.replace(':', '_')}_{os_module.getpid()}"
            click.echo("Bringing agent online (legacy single-agent mode)")
            click.echo(f"  Mode: {mode}")
            click.echo(f"  Agent: {display_name} ({resolved_id})")
            click.echo(f"  Recipe: {recipe}")
            click.echo(f"  A2A: http://{settings.agent_host}:{port}")

            resolved_cookbook = cookbook or settings.default_cookbook_id
            if not resolved_cookbook:
                raise click.UsageError("Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID")

            asyncio.run(run_agent(
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

        # Interactive mode: if no --recipe or --cookbook, prompt the human
        if not recipe or not resolved_cookbook:
            import shutil
            from krewcli.interactive import prompt_multi_select, prompt_single_select
            from krewcli.auth.token_store import load_token as _load_tok
            _tok = _load_tok()
            if not _tok:
                raise click.UsageError("No session. Run 'krewcli login' first.")

            _client = client_cls(
                settings.krewhub_url,
                settings.api_key,
                jwt_token=_tok,
                verify_ssl=settings.verify_ssl,
            )

            async def _fetch_cookbooks():
                cbs = await _client.list_cookbooks()
                await _client.close()
                return cbs

            async def _fetch_cookbook_detail(cb_id):
                detail = await _client.get_cookbook(cb_id)
                await _client.close()
                return detail

            click.echo("Fetching cookbooks...")
            cookbooks = asyncio.run(_fetch_cookbooks())
            if not cookbooks:
                raise click.UsageError("No cookbooks found. Create one in cookrew first.")

            cb_items = [(cb["name"], cb["id"]) for cb in cookbooks]
            cb_idx = prompt_single_select("Cookbooks", cb_items)
            selected_cb = cookbooks[cb_idx]
            resolved_cookbook = selected_cb["id"]

            # Refetch client for next call
            _client = client_cls(
                settings.krewhub_url,
                settings.api_key,
                jwt_token=_tok,
                verify_ssl=settings.verify_ssl,
            )
            cb_detail = asyncio.run(_fetch_cookbook_detail(resolved_cookbook))
            recipes_list = cb_detail.get("recipes", [])
            if not recipes_list:
                raise click.UsageError(f"No recipes in cookbook '{selected_cb['name']}'.")

            rec_items = [(r["name"], r["id"]) for r in recipes_list]
            rec_indices = prompt_multi_select("Recipes (select which to work on)", rec_items)
            selected_recipes = [recipes_list[i] for i in rec_indices]
            recipe = selected_recipes[0]["id"]

            # Detect agents on PATH
            available_agents = [name for name in AGENT_REGISTRY if shutil.which(name)]
            if not available_agents:
                raise click.UsageError("No agent CLIs found on PATH (claude, codex, etc).")

            agent_items = [(f"{name} ✓", name) for name in available_agents]
            agent_indices = prompt_multi_select("Agents (detected on PATH)", agent_items)
            selected_agent_names = [available_agents[i] for i in agent_indices]
            if selected_agent_names:
                agent_names = selected_agent_names

        if not resolved_cookbook:
            raise click.UsageError("Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID")
        if not recipe:
            raise click.UsageError("Specify --recipe or use interactive mode")

        click.echo("\nStarting A2A gateway")
        click.echo(f"  Recipe: {recipe}")
        click.echo(f"  Cookbook: {resolved_cookbook}")
        click.echo(f"  Work dir: {resolved_workdir}")
        click.echo(f"  Port: {port}")
        click.echo(f"  Max concurrent per agent: {max_concurrent}")
        click.echo(f"  KrewHub: {settings.krewhub_url}")

        asyncio.run(run_gateway(
            settings=settings,
            recipe_id=recipe,
            cookbook_id=resolved_cookbook,
            agent_id_prefix=agent_id or f"gw_{os_module.getpid()}",
            working_dir=resolved_workdir,
            agent_names=agent_names,
            max_concurrent=max_concurrent,
        ))

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
