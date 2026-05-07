"""``krewcli login`` — device-flow auth + bring agents online.

Mirrors multica's login UX: after the device flow returns a token, the
command continues into the same bootstrap as ``krewcli up`` so every
backend on PATH is registered with krewhub and the daemon is watching
for work. From the operator's perspective, ``krewcli login`` is the
single command that takes a fresh shell from "logged out" to "agents
online and ready to claim tasks".

Multica reference (apps/desktop/src/renderer/src/App.tsx):

    useEffect(() => {
      if (!user) return;
      const token = localStorage.getItem("multica_token");
      ...
      await window.daemonAPI.syncToken(token, userId);
      await window.daemonAPI.autoStart();
    }, [user]);

Multica's renderer auto-syncs the token to ``~/.multica/profiles/<p>/config.json``
and calls ``daemon:start`` once the user is logged in. The CLI equivalent
is to run that bootstrap inline. ``--no-start`` opts out for callers that
only want the token (CI provisioning, scripted token rotation, the
existing primitive-flow tests).
"""
from __future__ import annotations

import asyncio
import os
import shutil

import click

from krewcli.auth import device_flow, token_store
from krewcli.backend.registry import BACKEND_INFO, resolve_backends
from krewcli.cli.daemon import (
    _build_foreground_args,
    _make_sync_client,
    _now_iso,
    _run_daemon,
)
from krewcli.cli.up import _ensure_cookbook, _ensure_recipe
from krewcli.config import get_settings
from krewcli.daemon import supervisor


async def _login_token() -> dict:
    """Run a fresh device flow and persist the resulting token.

    Each ``krewcli login`` invocation produces a new device pair on
    purpose: the agent JWT is sandboxed to one daemon session, and
    rotating it on every pairing is good security hygiene. The
    *human* session stays alive across pairings via the
    ``krewauth_session`` cookie at auth.cookrew.dev — when the
    operator approves the new pair in the browser, no password
    re-entry is required as long as the cookie is still valid.
    """
    settings = get_settings()
    dc = await device_flow.request(settings.krew_auth_url)
    device_flow.display_code(dc)
    tok = await device_flow.poll(settings.krew_auth_url, dc.device_code)
    record = {
        "token": tok.token,
        "account_id": tok.account_id,
        "expires_at": tok.expires_at,
    }
    token_store.save_record(record)
    # Also write the legacy raw-token file so existing daemon code keeps working.
    token_store.save_token(tok.token)
    click.echo(f"Logged in as {tok.account_id}")
    return record


def _autodetect_backends() -> list[str]:
    """Return BACKEND_INFO entries whose CLI is on PATH (skipping echo)."""
    return [
        name for name in BACKEND_INFO
        if name != "echo" and shutil.which(name) is not None
    ]


@click.command("login")
@click.option(
    "--no-start",
    is_flag=True,
    default=False,
    help="Stop after auth — don't bring agents online (token-only mode).",
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Run the daemon in the foreground (blocking) instead of detaching.",
)
@click.option("--cookbook", default=None, help="Cookbook ID (optional, auto-resolved)")
@click.option("--recipe", default=None, help="Recipe ID (optional, auto-resolved)")
@click.option("--workdir", default=None, help="Working directory (default: cwd)")
@click.option(
    "--agents",
    default=None,
    help="Comma-separated backend names. Auto-detected from PATH if omitted.",
)
@click.option("--max-concurrent", default=5, type=int, help="Concurrent tasks per agent")
@click.option("--poll-interval", default=5.0, type=float)
@click.pass_context
def login_cmd(
    ctx: click.Context,
    no_start: bool,
    foreground: bool,
    cookbook: str | None,
    recipe: str | None,
    workdir: str | None,
    agents: str | None,
    max_concurrent: int,
    poll_interval: float,
) -> None:
    """Device-flow login, then bring every detected agent online.

    \b
    Examples:
      krewcli login                  # auth + spawn daemon in background, exit
      krewcli login --foreground     # auth + run daemon attached to this shell
      krewcli login --no-start       # auth only, no daemon
      krewcli login --agents claude  # only run claude

    Default mode mirrors multica's UX: after the device flow, the daemon
    is spawned as a detached child (``~/.krewcli/daemon.pid``,
    ``daemon.log``) and ``login`` exits cleanly so the operator can keep
    using the terminal. Inspect the daemon with ``krewcli daemon
    status``; stop it with ``krewcli daemon stop``.
    """
    settings = ctx.obj["settings"] if ctx.obj and "settings" in ctx.obj else get_settings()

    # 1) Always run a fresh device flow — each `krewcli login` is one
    #    pairing session. The human's web session at auth.cookrew.dev
    #    persists across pairings via cookie auto-resume, so the
    #    operator doesn't re-enter a password unless the cookie has
    #    actually expired.
    record = asyncio.run(_login_token())

    if no_start:
        return

    # 2) Resolve backends. Same auto-detect rule as ``krewcli up``.
    if agents:
        requested = [a.strip() for a in agents.split(",") if a.strip()]
    else:
        requested = _autodetect_backends()
        if not requested:
            click.echo(
                "Login complete, but no coding agents found on PATH "
                "(claude, codex, gemini, bub). Install one — or pass "
                "--agents echo for testing — then re-run `krewcli login` "
                "to bring agents online.",
                err=True,
            )
            return
    backends = resolve_backends(requested)
    if not backends:
        click.echo(
            "Login complete, but no backends could be resolved.",
            err=True,
        )
        return

    resolved_workdir = os.path.abspath(workdir or os.getcwd())

    # 3) Idempotent — if a daemon is already running for this user, skip
    #    the bootstrap and let the running daemon pick up the new token
    #    via SSEWatcher's token_reloader. Mirrors multica's syncToken
    #    behaviour: when a logged-in renderer re-runs login, the existing
    #    daemon stays up.
    existing = supervisor.read_status()
    if existing:
        click.echo(
            f"Daemon already running (pid {existing['pid']}); "
            f"new token saved — agents stay online."
        )
        if existing.get("agents"):
            click.echo(f"  Agents: {', '.join(existing['agents'])}")
        click.echo("  Use `krewcli daemon status` for details.")
        return

    # 4) Resolve cookbook/recipe via the same helpers ``krewcli up`` uses.
    async def _bootstrap():
        client = _make_sync_client(settings)
        try:
            cb = await _ensure_cookbook(client, record["account_id"], cookbook)
            rec = await _ensure_recipe(client, record["account_id"], cb, recipe)
            return cb, rec
        finally:
            await client.close()

    cb_id, rec_id = asyncio.run(_bootstrap())

    # 5) Foreground path — block on the daemon loop (same as krewcli up).
    if foreground:
        click.echo("")
        click.echo("krewcli login — agents online, watching for work")
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
        return

    # 6) Background (default) — fork a child running daemon start
    #    --foreground with the resolved config, exit cleanly. The child
    #    writes its own ready marker once agents are registered.
    child_args = _build_foreground_args(
        cookbook_id=cb_id,
        recipe_id=rec_id,
        workdir=resolved_workdir,
        agents=list(backends.keys()),
        max_concurrent=max_concurrent,
        poll_interval=poll_interval,
        repo_url="",
        branch="main",
    )
    supervisor.write_status({
        "cookbook_id": cb_id,
        "recipe_id": rec_id,
        "agents": list(backends.keys()),
        "workdir": resolved_workdir,
        "started_at": _now_iso(),
        "ready": False,
    })
    pid = supervisor.spawn_detached(child_args)

    click.echo("")
    click.echo("krewcli login — agents online")
    click.echo(f"  KrewHub:    {settings.krewhub_url}")
    click.echo(f"  Cookbook:   {cb_id}")
    click.echo(f"  Recipe:     {rec_id}")
    click.echo(f"  Agents:     {', '.join(backends.keys())}")
    click.echo(f"  Work dir:   {resolved_workdir}")
    click.echo(f"  Daemon pid: {pid}")
    click.echo(f"  Logs:       {supervisor.log_path()}")

    if not supervisor.wait_until_ready(pid):
        click.echo(
            "  WARNING: daemon did not signal ready within 15s. Check "
            "the log above and `krewcli daemon status`.",
            err=True,
        )


def register(group: click.Group) -> None:
    group.add_command(login_cmd)
