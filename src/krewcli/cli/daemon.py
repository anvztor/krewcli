"""CLI commands for the managed agent daemon.

``krewcli daemon start`` replaces the old ``krewcli join --gateway``
workflow with a simpler pull-based daemon that polls krewhub for tasks.

When ``--cookbook`` and ``--recipe`` are omitted, interactive prompts
guide the user through cookbook/recipe/agent selection — preserving
the UX from the old ``join`` command.
"""

from __future__ import annotations

import asyncio
import shutil

import click

from krewcli.client.krewhub_client import KrewHubClient


def register_daemon_commands(group: click.Group) -> None:
    """Register the ``daemon`` command group."""
    group.add_command(daemon)


@click.group()
def daemon() -> None:
    """Manage the krewcli daemon."""


@daemon.command()
@click.option("--cookbook", default=None, help="Cookbook ID (interactive if omitted)")
@click.option("--recipe", default=None, help="Recipe ID (interactive if omitted)")
@click.option("--workdir", default=".", help="Working directory for agent execution")
@click.option(
    "--agents",
    default=None,
    help="Comma-separated backend names (interactive if omitted). e.g. claude,codex,echo",
)
@click.option("--max-concurrent", default=1, type=int, help="Max concurrent task executions")
@click.option("--poll-interval", default=5.0, type=float, help="Seconds between polls")
@click.option("--repo-url", default="", help="Repository URL for code ref tracking")
@click.option("--branch", default="", help="Branch name for code ref tracking")
@click.option(
    "--background/--foreground",
    "background",
    default=False,
    help=(
        "Run the daemon detached (default: foreground, blocking). "
        "Background mode forks a child process, writes ~/.krewcli/daemon.pid, "
        "and exits — same UX as multica daemon start."
    ),
)
@click.pass_context
def start(
    ctx: click.Context,
    cookbook: str | None,
    recipe: str | None,
    workdir: str,
    agents: str | None,
    max_concurrent: int,
    poll_interval: float,
    repo_url: str,
    branch: str,
    background: bool,
) -> None:
    """Start the daemon. Polls krewhub for tasks and executes them.

    \b
    When --cookbook or --recipe are omitted, an interactive prompt
    guides you through selecting them from your krewhub account.

    \b
    Examples:
      krewcli daemon start                           # fully interactive
      krewcli daemon start --cookbook CB --recipe R   # non-interactive
      krewcli daemon start --cookbook CB --recipe R --agents echo  # test mode
    """
    import os

    from krewcli.backend.registry import resolve_backends, BACKEND_INFO
    from krewcli.interactive import prompt_multi_select, prompt_single_select

    settings = ctx.obj["settings"]
    resolved_workdir = os.path.abspath(workdir)

    resolved_cookbook = cookbook or settings.default_cookbook_id
    resolved_recipe = recipe
    need_interactive = not resolved_cookbook or not resolved_recipe

    # ── Interactive selection (uses a temporary sync client) ──────

    if need_interactive:
        # Use a dedicated event loop + client for interactive fetches
        # so it doesn't conflict with the daemon's loop later.
        _loop = asyncio.new_event_loop()
        _client = _make_sync_client(settings)

        try:
            if not resolved_cookbook:
                cookbooks = _loop.run_until_complete(_client.list_cookbooks())
                if not cookbooks:
                    raise click.ClickException(
                        "No cookbooks found. Create one in cookrew first."
                    )
                cb_items = [(cb["name"], cb["id"]) for cb in cookbooks]
                cb_idx = prompt_single_select("Cookbooks", cb_items)
                resolved_cookbook = cookbooks[cb_idx]["id"]

            if not resolved_recipe:
                detail = _loop.run_until_complete(
                    _client.get_cookbook(resolved_cookbook),
                )
                recipes = detail.get("recipes", [])
                if not recipes:
                    raise click.ClickException(
                        f"No recipes in cookbook '{resolved_cookbook}'. "
                        "Create one in cookrew first."
                    )
                rec_items = [(r.get("name", r["id"]), r["id"]) for r in recipes]
                if len(rec_items) == 1:
                    rec_idx = 0
                    click.echo(
                        f"\nRecipe: {rec_items[0][0]} ({rec_items[0][1]}) [auto-selected]"
                    )
                else:
                    rec_idx = prompt_single_select("Recipes", rec_items)
                resolved_recipe = recipes[rec_idx]["id"]
        finally:
            _loop.run_until_complete(_client.close())
            _loop.close()

    # Agent selection
    if agents is not None:
        requested = [a.strip() for a in agents.split(",")]
    else:
        detected = [
            name for name in BACKEND_INFO
            if name == "echo" or shutil.which(name) is not None
        ]
        if not detected:
            raise click.ClickException(
                "No agent CLIs found on PATH (claude, codex, bub). "
                "Install one or use --agents echo for testing."
            )
        agent_items = [
            (BACKEND_INFO.get(n, {}).get("display_name", n), n)
            for n in detected
        ]
        selected_indices = prompt_multi_select("Agents (detected on PATH)", agent_items)
        requested = [detected[i] for i in selected_indices]

    backends = resolve_backends(requested)
    if not backends:
        raise click.ClickException("No backends resolved.")

    # ── Background mode: fork a child running the same command with
    #    --foreground and exit. Mirrors multica's daemon start UX. ───

    if background:
        from krewcli.daemon import supervisor

        existing = supervisor.read_status()
        if existing:
            raise click.ClickException(
                f"Daemon already running (pid {existing['pid']}). "
                f"Run `krewcli daemon stop` first."
            )

        child_args = _build_foreground_args(
            cookbook_id=resolved_cookbook,
            recipe_id=resolved_recipe,
            workdir=resolved_workdir,
            agents=list(backends.keys()),
            max_concurrent=max_concurrent,
            poll_interval=poll_interval,
            repo_url=repo_url,
            branch=branch,
        )
        # Seed status before fork so `daemon status` works even if the
        # child takes a moment to write its own ready marker.
        supervisor.write_status({
            "cookbook_id": resolved_cookbook,
            "recipe_id": resolved_recipe,
            "agents": list(backends.keys()),
            "workdir": resolved_workdir,
            "started_at": _now_iso(),
            "ready": False,
        })
        pid = supervisor.spawn_detached(child_args)
        click.echo(f"\nkrewcli daemon spawned (pid {pid})")
        click.echo(f"  Logs: {supervisor.log_path()}")

        if supervisor.wait_until_ready(pid):
            click.echo(
                f"  Agents online: {', '.join(backends.keys())} — "
                f"cookbook {resolved_cookbook}, recipe {resolved_recipe}"
            )
        else:
            click.echo(
                "  Daemon did not signal ready within 15s — check the log "
                "above for errors. Use `krewcli daemon status` to recheck.",
                err=True,
            )
        return

    # ── Foreground mode: write status sidecar then block on the loop. ─

    from krewcli.daemon import supervisor

    supervisor.write_status({
        "cookbook_id": resolved_cookbook,
        "recipe_id": resolved_recipe,
        "agents": list(backends.keys()),
        "workdir": resolved_workdir,
        "started_at": _now_iso(),
        "ready": False,
    })
    supervisor.write_pid(os.getpid())

    click.echo("\nkrewcli daemon starting")
    click.echo(f"  KrewHub:    {settings.krewhub_url}")
    click.echo(f"  Cookbook:    {resolved_cookbook}")
    click.echo(f"  Recipe:     {resolved_recipe}")
    click.echo(f"  Agents:     {', '.join(backends.keys())}")
    click.echo(f"  Work dir:   {resolved_workdir}")
    click.echo(f"  Concurrent: {max_concurrent}")

    try:
        asyncio.run(_run_daemon(
            settings=settings,
            backends=backends,
            cookbook_id=resolved_cookbook,
            recipe_id=resolved_recipe,
            working_dir=resolved_workdir,
            repo_url=repo_url,
            branch=branch,
            max_concurrent=max_concurrent,
            poll_interval=poll_interval,
        ))
    finally:
        supervisor.clear()


async def _run_daemon(
    settings,
    backends,
    cookbook_id: str,
    recipe_id: str,
    working_dir: str,
    repo_url: str,
    branch: str,
    max_concurrent: int,
    poll_interval: float,
) -> None:
    """Create a fresh client and run the daemon loop."""
    from krewcli.auth.token_store import load_token
    from krewcli.daemon.loop import DaemonLoop

    jwt_token = load_token()
    client = KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=jwt_token,
        verify_ssl=settings.verify_ssl,
    )

    loop = DaemonLoop(
        client=client,
        backends=backends,
        cookbook_id=cookbook_id,
        recipe_id=recipe_id,
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        max_concurrent=max_concurrent,
        poll_interval=poll_interval,
    )

    try:
        await loop.run()
    except KeyboardInterrupt:
        click.echo("\nDaemon stopped.")
    finally:
        await client.close()


@daemon.command()
def status() -> None:
    """Show daemon status — pid, agents, cookbook, recipe."""
    from krewcli.daemon import supervisor

    info = supervisor.read_status()
    if info is None:
        click.echo("Daemon: stopped")
        return
    click.echo(f"Daemon:    running (pid {info['pid']})")
    if info.get("started_at"):
        click.echo(f"Started:   {info['started_at']}")
    if info.get("cookbook_id"):
        click.echo(f"Cookbook:  {info['cookbook_id']}")
    if info.get("recipe_id"):
        click.echo(f"Recipe:    {info['recipe_id']}")
    if info.get("agents"):
        click.echo(f"Agents:    {', '.join(info['agents'])}")
    if info.get("workdir"):
        click.echo(f"Workdir:   {info['workdir']}")
    click.echo(f"Logs:      {supervisor.log_path()}")


@daemon.command()
def stop() -> None:
    """Stop the running daemon (SIGTERM, SIGKILL fallback)."""
    from krewcli.daemon import supervisor

    info = supervisor.read_status()
    if info is None:
        click.echo("Daemon: not running")
        return
    pid = info["pid"]
    if supervisor.stop():
        click.echo(f"Daemon stopped (pid {pid})")
    else:
        raise click.ClickException(
            f"Failed to stop daemon (pid {pid}); check process state manually."
        )


# ── Helpers ──────────────────────────────────────────────────────


def _make_sync_client(settings) -> KrewHubClient:
    """Create a temporary KrewHubClient for interactive selection.

    Uses a separate instance so it doesn't interfere with the daemon's
    event loop. Callers must close it when done.
    """
    from krewcli.auth.token_store import load_token
    return KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=load_token(),
        verify_ssl=settings.verify_ssl,
    )


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 (seconds resolution)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_foreground_args(
    *,
    cookbook_id: str,
    recipe_id: str,
    workdir: str,
    agents: list[str],
    max_concurrent: int,
    poll_interval: float,
    repo_url: str,
    branch: str,
) -> list[str]:
    """Build the argv tail passed to the spawned `daemon start --foreground`.

    Mirrors multica's buildDaemonStartArgs — every choice the parent
    resolved (cookbook, recipe, agents, workdir) is forwarded to the
    child so it doesn't re-prompt or re-detect.
    """
    args: list[str] = ["--foreground"]
    if cookbook_id:
        args += ["--cookbook", cookbook_id]
    if recipe_id:
        args += ["--recipe", recipe_id]
    if workdir:
        args += ["--workdir", workdir]
    if agents:
        args += ["--agents", ",".join(agents)]
    if max_concurrent:
        args += ["--max-concurrent", str(max_concurrent)]
    if poll_interval:
        args += ["--poll-interval", str(poll_interval)]
    if repo_url:
        args += ["--repo-url", repo_url]
    if branch:
        args += ["--branch", branch]
    return args


