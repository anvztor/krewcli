from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click
import httpx
import uvicorn

from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.auth.token_store import save_token
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
    ctx.obj["client"] = KrewHubClient(settings.krewhub_url, settings.api_key)


# ── join: the universal agent onboarding command ──


@main.command()
@click.option("--recipe", required=True, help="Recipe ID to join")
@click.option("--cookbook", default=None, help="Cookbook ID (required for agent registration)")
@click.option("--port", default=9999, type=int, help="A2A server port")
@click.option("--agent-id", default=None, help="Override agent ID")
@click.option("--workdir", default=".", help="Working directory for agent")
# Tier 1: direct LLM
@click.option("--provider", type=click.Choice(["anthropic", "openai"]), default=None, help="LLM provider for direct call")
@click.option("--model", default=None, help="Override model name")
# Tier 2: CLI agent
@click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default=None, help="CLI agent backend")
# Tier 2: remote endpoint
@click.option("--endpoint", default=None, help="Remote A2A agent URL")
# Tier 2: framework agent
@click.option("--framework", type=click.Choice(["anthropic", "openai"]), default=None, help="pydantic-ai framework agent")
# Tier 3: orchestrator
@click.option("--orchestrator", is_flag=True, default=False, help="Run as orchestrator")
@click.pass_context
def join(ctx, recipe, cookbook, port, agent_id, workdir, provider, model, agent, endpoint, framework, orchestrator):
    """Bring an agent online on Cookrew.

    Each invocation creates ONE independent agent with its own AgentCard.
    Run multiple agents on different ports.

    \b
    Tier 1 — Direct LLM (stateless):
      krewcli join --recipe ID --provider anthropic

    \b
    Tier 2 — Agent (stateful, modifies code):
      krewcli join --recipe ID --agent claude
      krewcli join --recipe ID --framework anthropic
      krewcli join --recipe ID --endpoint http://my-agent:8080

    \b
    Tier 3 — Orchestrator (decomposes & dispatches):
      krewcli join --recipe ID --orchestrator --provider anthropic
    """
    settings = ctx.obj["settings"]
    settings = settings.model_copy(update={"agent_port": port})
    resolved_workdir = os.path.abspath(workdir)

    mode, executor, card, display_name, caps = _resolve_mode(
        agent=agent, provider=provider, model=model, framework=framework,
        endpoint=endpoint, orchestrator=orchestrator,
        host=settings.agent_host, port=port, working_dir=resolved_workdir,
        settings=settings,
    )

    resolved_id = agent_id or f"{mode.replace(':', '_')}_{os.getpid()}"

    click.echo("Bringing agent online on Cookrew")
    click.echo(f"  Mode: {mode}")
    click.echo(f"  Agent: {display_name} ({resolved_id})")
    click.echo(f"  Recipe: {recipe}")
    click.echo(f"  Work dir: {resolved_workdir}")
    click.echo(f"  A2A: http://{settings.agent_host}:{port}")
    click.echo(f"  KrewHub: {settings.krewhub_url}")

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
        executor = OrchestratorExecutor()
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

    worker_task = None
    if mode.startswith("cli:") and agent_name:
        worker_task = asyncio.create_task(_run_task_worker(settings=settings, client=client, heartbeat=heartbeat, recipe_id=recipe_id, agent_name=agent_name, agent_id=agent_id, working_dir=working_dir))

    auth_service = None
    if not settings.jwt_secret:
        logging.getLogger(__name__).warning(
            "KREWCLI_JWT_SECRET is not set — auth is DISABLED, all endpoints are public"
        )
    elif len(settings.jwt_secret) < 32:
        logging.getLogger(__name__).warning(
            "KREWCLI_JWT_SECRET is set but shorter than 32 chars — auth disabled"
        )
    else:
        from krewcli.auth.service import AuthService
        auth_service = AuthService(
            jwt_secret=settings.jwt_secret,
            token_expiry_minutes=settings.token_expiry_minutes,
        )
        logging.getLogger(__name__).info("Auth enabled (JWT middleware active)")

    app = create_a2a_app(agent_card=card, executor=executor, auth_service=auth_service)
    config = uvicorn.Config(app, host=settings.agent_host, port=settings.agent_port, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await heartbeat.stop()
        await client.close()


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


# ── onboard: hook-based agent monitoring ──


@main.command()
@click.option("--cookbook", default=None, help="Cookbook ID (creates one if not set)")
@click.option("--cookbook-name", default="my-cookbook", help="Name for new cookbook")
@click.option("--recipe", default=None, help="Recipe ID to monitor")
@click.option("--agent-id", default=None, help="Override agent ID")
@click.option("--port", default=None, type=int, help="Hook listener port")
@click.option("--owner", default="cli_user", help="Owner ID for cookbook creation")
@click.pass_context
def onboard(ctx, cookbook, cookbook_name, recipe, agent_id, port, owner):
    """Onboard agents with hook-based event streaming.

    Detects Claude/Codex on PATH, configures their hooks to report events
    to KrewHub. Agents run in their own terminals — KrewCLI just listens.

    \b
    Steps:
      1. Create or select a cookbook
      2. Detect available agents (claude, codex)
      3. Configure hook files (~/.claude/settings.json, etc.)
      4. Start hook listener (HTTP)
      5. Forward events to KrewHub
    """
    import shutil

    settings = ctx.obj["settings"]
    listener_port = port or settings.hook_listener_port

    # Detect agents
    agents_found = []
    if shutil.which("claude"):
        agents_found.append(("claude", "Claude Code", ["code", "implement", "fix"]))
    if shutil.which("codex"):
        agents_found.append(("codex", "Codex CLI", ["review", "code"]))

    if not agents_found:
        click.echo("No agents found on PATH (checked: claude, codex)")
        raise SystemExit(1)

    click.echo(f"Detected agents: {', '.join(a[0] for a in agents_found)}")

    resolved_id = agent_id or f"krew_{os.getpid()}"
    listener_url = f"http://127.0.0.1:{listener_port}"

    asyncio.run(_run_onboard(
        settings=settings,
        cookbook_id=cookbook,
        cookbook_name=cookbook_name,
        recipe_id=recipe,
        agent_id=resolved_id,
        owner_id=owner,
        listener_url=listener_url,
        listener_port=listener_port,
        agents_found=agents_found,
    ))


async def _run_onboard(settings, cookbook_id, cookbook_name, recipe_id, agent_id, owner_id, listener_url, listener_port, agents_found):
    from krewcli.hooks.config_writer import configure_claude_hooks, configure_codex_hooks, remove_claude_hooks, remove_codex_hooks
    from krewcli.hooks.listener import create_hook_listener_app
    from krewcli.hooks.spawner import spawn_agent, kill_agent_session

    client = KrewHubClient(settings.krewhub_url, settings.api_key)
    spawned_sessions: list[str] = []

    # 1. Create or verify cookbook
    if not cookbook_id:
        try:
            cb = await client.create_cookbook(name=cookbook_name, owner_id=owner_id)
            cookbook_id = cb["id"]
            click.echo(f"Created cookbook: {cookbook_id}")
        except Exception as exc:
            click.echo(f"Failed to create cookbook: {exc}")
            await client.close()
            raise SystemExit(1)

    # 2. Register agents with KrewHub
    for agent_name, display_name, caps in agents_found:
        aid = f"{agent_id}_{agent_name}"
        try:
            await client.register_agent(
                agent_id=aid,
                cookbook_id=cookbook_id,
                display_name=display_name,
                capabilities=caps,
            )
            click.echo(f"  Registered {display_name} ({aid})")
        except Exception as exc:
            click.echo(f"  Warning: registration failed for {agent_name}: {exc}")

    # 3. Configure hooks (before spawning so agents pick them up)
    for agent_name, _, _ in agents_found:
        if agent_name == "claude":
            if configure_claude_hooks(listener_url):
                click.echo("  Configured Claude hooks")
        elif agent_name == "codex":
            if configure_codex_hooks(listener_url):
                click.echo("  Configured Codex hooks")

    # 4. Spawn agents with greeting prompt
    workdir = os.path.abspath(".")
    for agent_name, display_name, _ in agents_found:
        aid = f"{agent_id}_{agent_name}"
        session = await spawn_agent(
            agent_name=agent_name,
            agent_id=aid,
            workdir=workdir,
        )
        if session:
            spawned_sessions.append(session)
            click.echo(f"  Spawned {display_name} -> {session}")
        else:
            click.echo(f"  Warning: failed to spawn {display_name}")

    # 5. Start heartbeat
    primary_aid = f"{agent_id}_{agents_found[0][0]}"
    heartbeat = HeartbeatLoop(
        client=client, agent_id=primary_aid, cookbook_id=cookbook_id,
        display_name=agents_found[0][1],
        capabilities=agents_found[0][2],
        interval=settings.heartbeat_interval,
    )
    heartbeat.start()

    # 6. Start hook listener (cookbook-aware, routes events to correct recipe)
    hook_app = create_hook_listener_app(
        client=client,
        cookbook_id=cookbook_id,
        agent_id=primary_aid,
        default_recipe_id=recipe_id or "",
    )

    click.echo(f"\nOnboarding complete:")
    click.echo(f"  Cookbook: {cookbook_id}")
    click.echo(f"  Agents: {', '.join(a[0] for a in agents_found)}")
    click.echo(f"  Sessions: {', '.join(spawned_sessions)}")
    click.echo(f"  Hook listener: {listener_url}")
    click.echo(f"  KrewHub: {settings.krewhub_url}")
    click.echo(f"\nAgents are alive. Events streaming. Press Ctrl+C to stop.")

    config = uvicorn.Config(hook_app, host="127.0.0.1", port=listener_port, log_level="warning")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        # Cleanup: kill spawned sessions, remove hooks
        for session in spawned_sessions:
            await kill_agent_session(session)
        for agent_name, _, _ in agents_found:
            if agent_name == "claude":
                remove_claude_hooks()
            elif agent_name == "codex":
                remove_codex_hooks()
        await heartbeat.stop()
        await client.close()
        click.echo("Agents stopped. Hooks removed. Goodbye.")


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


@main.command("register")
@click.option("--url", default=None, help="Auth server URL (default: http://{agent_host}:{agent_port})")
@click.pass_context
def register(ctx, url):
    """Register a new user account."""
    settings = ctx.obj["settings"]
    base_url = url or f"http://{settings.agent_host}:{settings.agent_port}"

    email = click.prompt("Email")
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)

    try:
        with httpx.Client(timeout=10) as http:
            resp = http.post(
                f"{base_url}/auth/register",
                json={"email": email, "password": password},
            )
    except httpx.ConnectError:
        click.echo(f"Error: Could not connect to server at {base_url}. Is 'krewcli join' running?", err=True)
        raise SystemExit(1)

    if resp.status_code == 201:
        click.echo(f"Registered as {email}")
    else:
        error = resp.json().get("error", "Registration failed")
        click.echo(f"Error: {error}", err=True)
        raise SystemExit(1)


@main.command("login")
@click.option("--url", default=None, help="Auth server URL (default: http://{agent_host}:{agent_port})")
@click.pass_context
def login(ctx, url):
    """Authenticate and save an access token."""
    settings = ctx.obj["settings"]
    base_url = url or f"http://{settings.agent_host}:{settings.agent_port}"

    email = click.prompt("Email")
    password = click.prompt("Password", hide_input=True)

    try:
        with httpx.Client(timeout=10) as http:
            resp = http.post(
                f"{base_url}/auth/login",
                json={"email": email, "password": password},
            )
    except httpx.ConnectError:
        click.echo(f"Error: Could not connect to server at {base_url}. Is 'krewcli join' running?", err=True)
        raise SystemExit(1)

    if resp.status_code == 200:
        data = resp.json()
        save_token(data["access_token"])
        click.echo("Logged in. Token saved.")
    else:
        error = resp.json().get("error", "Login failed")
        click.echo(f"Error: {error}", err=True)
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
