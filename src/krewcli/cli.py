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
def join(ctx, recipe, port, agent_id, workdir, provider, model, agent, endpoint, framework, orchestrator):
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

    asyncio.run(_run_agent(
        settings=settings, recipe_id=recipe, agent_id=resolved_id,
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
        p = provider or "anthropic"
        m = model or _default_model(p)
        executor = OrchestratorExecutor(model=f"{p}:{m}", krewhub_url=settings.krewhub_url, api_key=settings.api_key)
        card = build_orchestrator_card(host, port)
        return "orchestrator", executor, card, "Orchestrator", ["orchestrate", "decompose", "coordinate"]

    raise click.UsageError("Specify one of: --agent, --provider, --framework, --endpoint, or --orchestrator")


def _default_model(provider):
    return {"anthropic": "claude-sonnet-4-20250514", "openai": "gpt-4o"}.get(provider, "claude-sonnet-4-20250514")


async def _run_agent(settings, recipe_id, agent_id, display_name, capabilities, executor, card, working_dir, mode, agent_name=None):
    from krewcli.a2a.server import create_a2a_app
    client = KrewHubClient(settings.krewhub_url, settings.api_key)

    try:
        await client.register_agent(agent_id=agent_id, recipe_id=recipe_id, display_name=display_name, capabilities=capabilities)
    except Exception:
        logging.getLogger(__name__).warning("Registration failed, continuing with heartbeat")

    heartbeat = HeartbeatLoop(client=client, agent_id=agent_id, recipe_id=recipe_id, display_name=display_name, capabilities=capabilities, interval=settings.heartbeat_interval)
    heartbeat.start()

    worker_task = None
    if mode.startswith("cli:") and agent_name:
        worker_task = asyncio.create_task(_run_task_worker(settings=settings, client=client, heartbeat=heartbeat, recipe_id=recipe_id, agent_name=agent_name, agent_id=agent_id, working_dir=working_dir))

    a2a_app = create_a2a_app(agent_card=card, executor=executor)
    config = uvicorn.Config(a2a_app.build(), host=settings.agent_host, port=settings.agent_port, log_level="info")
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


# ── Legacy start command ──


@main.command()
@click.option("--recipe", required=True)
@click.option("--agent", type=click.Choice(list(AGENT_REGISTRY.keys())), default="claude")
@click.option("--agent-id", default=None)
@click.option("--port", default=9999, type=int)
@click.option("--workdir", default=".")
@click.option("--mode", type=click.Choice(["poll", "watch"]), default="poll", hidden=True)
@click.pass_context
def start(ctx, recipe, agent, agent_id, port, workdir, mode):
    """[Legacy] Start an agent. Use 'join' instead."""
    ctx.invoke(join, recipe=recipe, agent=agent, agent_id=agent_id, port=port, workdir=workdir)


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


if __name__ == "__main__":
    main()
