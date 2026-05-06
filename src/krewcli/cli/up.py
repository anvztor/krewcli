"""``krewcli up`` — one-shot login + agent registration + task daemon.

The single command users need to bring agents online for cookrew-beta:

    $ krewcli up

Resolves everything that the multi-step setup used to require:

  1. Login: runs the device flow against krewauth if no fresh JWT is
     on disk; otherwise reuses ~/.krewcli/token.
  2. Cookbook + recipe: picks the most-recent owned cookbook (or
     creates "my-cookbook" + "my-recipe" if the account has none).
  3. Backends: auto-detects every coding CLI on PATH (claude, codex,
     gemini, bub) — no --agents flag needed unless you want to
     override.
  4. Workdir: defaults to the current directory.
  5. Daemon: registers each backend as both an agent_presence row
     AND an agent_runtimes row so cookrew-beta's roster sees the
     daemon live, then blocks accepting tasks until Ctrl-C.

Existing escape-hatches (``krewcli login``, ``krewcli daemon start``,
etc.) keep working for non-default flows; ``up`` is the documented
entry point for "I just want to bind my agents to cookrew-beta and
go."
"""
from __future__ import annotations

import asyncio
import os
import shutil

import click

from krewcli.auth import device_flow, token_store
from krewcli.backend.registry import BACKEND_INFO, resolve_backends
from krewcli.cli.daemon import _make_sync_client, _run_daemon


async def _ensure_login(krew_auth_url: str) -> dict:
    """Return a fresh login record; runs the device flow if missing."""
    rec = token_store.load_record()
    if rec and rec.get("token"):
        return rec
    click.echo("Not logged in — starting passkey/device flow against krewauth…", err=True)
    dc = await device_flow.request(krew_auth_url)
    device_flow.display_code(dc)
    tok = await device_flow.poll(krew_auth_url, dc.device_code)
    rec = {
        "token": tok.token,
        "account_id": tok.account_id,
        "expires_at": tok.expires_at,
    }
    token_store.save_record(rec)
    token_store.save_token(tok.token)
    click.echo(f"Logged in as {tok.account_id}")
    return rec


async def _ensure_cookbook(client, account_id: str, requested: str | None) -> str:
    """Pick or create a cookbook owned by the caller."""
    if requested:
        return requested
    cookbooks = await client.list_cookbooks()
    owned = [c for c in cookbooks if c.get("owner_id") == account_id]
    if owned:
        cb = owned[0]
        click.echo(f"  Cookbook: {cb['id']}  ({cb.get('name', '')})")
        return cb["id"]

    click.echo("  No owned cookbook — creating 'my-cookbook'…")
    resp = await client._client.post(
        "/api/v1/cookbooks",
        json={"name": "my-cookbook", "owner_id": account_id},
    )
    resp.raise_for_status()
    cb_id = resp.json()["cookbook"]["id"]
    click.echo(f"  Cookbook: {cb_id}  (created)")
    return cb_id


async def _ensure_recipe(client, account_id: str, cookbook_id: str, requested: str | None) -> str:
    """Pick or create a recipe inside the cookbook."""
    if requested:
        return requested
    detail = await client.get_cookbook(cookbook_id)
    recipes = detail.get("recipes", []) or []
    if recipes:
        rec = recipes[0]
        click.echo(f"  Recipe:   {rec['id']}  ({rec.get('name', '')})")
        return rec["id"]

    click.echo("  No recipe in cookbook — creating 'my-recipe'…")
    rec = await client.create_recipe(
        name="my-recipe",
        repo_url="",
        created_by=account_id,
        cookbook_id=cookbook_id,
    )
    click.echo(f"  Recipe:   {rec['id']}  (created)")
    return rec["id"]


def _autodetect_backends() -> list[str]:
    """Return every BACKEND_INFO entry whose CLI is on PATH (skip echo)."""
    return [
        name for name in BACKEND_INFO
        if name != "echo" and shutil.which(name) is not None
    ]


@click.command("up")
@click.option("--cookbook", default=None, help="Cookbook ID (optional, auto-resolved)")
@click.option("--recipe", default=None, help="Recipe ID (optional, auto-resolved)")
@click.option("--workdir", default=None, help="Working directory (default: cwd)")
@click.option(
    "--agents",
    default=None,
    help="Comma-separated backend names. Auto-detected if omitted.",
)
@click.option("--max-concurrent", default=5, type=int, help="Concurrent tasks per agent")
@click.option("--poll-interval", default=5.0, type=float)
@click.pass_context
def up_cmd(
    ctx: click.Context,
    cookbook: str | None,
    recipe: str | None,
    workdir: str | None,
    agents: str | None,
    max_concurrent: int,
    poll_interval: float,
) -> None:
    """One command: log in, register agents, accept tasks for cookrew-beta.

    \b
    Examples:
      krewcli up                   # auto-everything; just go
      krewcli up --agents claude   # only run claude
      krewcli up --workdir /repo   # bind tasks to a specific repo
    """
    settings = ctx.obj["settings"]
    resolved_workdir = os.path.abspath(workdir or os.getcwd())

    # 1) Resolve backends (autodetect if not specified)
    if agents:
        requested = [a.strip() for a in agents.split(",") if a.strip()]
    else:
        requested = _autodetect_backends()
        if not requested:
            raise click.ClickException(
                "No coding agents found on PATH. Install one of: "
                + ", ".join(n for n in BACKEND_INFO if n != "echo")
            )
    backends = resolve_backends(requested)
    if not backends:
        raise click.ClickException("No backends resolved")

    # 2) Login + cookbook/recipe resolution (uses an isolated event loop
    #    so the daemon's loop later doesn't inherit a half-used client).
    async def _bootstrap():
        record = await _ensure_login(settings.krew_auth_url)
        client = _make_sync_client(settings)
        try:
            cb = await _ensure_cookbook(client, record["account_id"], cookbook)
            rec = await _ensure_recipe(client, record["account_id"], cb, recipe)
            return cb, rec
        finally:
            await client.close()

    cb_id, rec_id = asyncio.run(_bootstrap())

    # 3) Print resolved config + start the daemon
    click.echo("")
    click.echo("krewcli up — agents bound to cookrew-beta")
    click.echo(f"  KrewHub:    {settings.krewhub_url}")
    click.echo(f"  Cookbook:   {cb_id}")
    click.echo(f"  Recipe:     {rec_id}")
    click.echo(f"  Agents:     {', '.join(backends.keys())}")
    click.echo(f"  Work dir:   {resolved_workdir}")
    click.echo(f"  Concurrent: {max_concurrent}")
    click.echo("")

    asyncio.run(_run_daemon(
        settings=settings,
        backends=backends,
        cookbook_id=cb_id,
        recipe_id=rec_id,
        working_dir=resolved_workdir,
        repo_url="",
        branch="main",
        max_concurrent=max_concurrent,
        poll_interval=poll_interval,
    ))


def register(group: click.Group) -> None:
    group.add_command(up_cmd)
