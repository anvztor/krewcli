"""KrewCLI command-line interface package.

Entry point: ``krewcli.cli:main``
"""

from __future__ import annotations

import logging
import os

import click
import httpx

from krewcli.client.krewhub_client import KrewHubClient
from krewcli.config import get_settings
from krewcli.gateway.identity import _gateway_agent_metadata

# ── Shared CLI group with friendly error handling ──


class _KrewCLI(click.Group):
    """Click group with friendly error handling."""

    def invoke(self, ctx: click.Context) -> None:
        try:
            super().invoke(ctx)
        except click.exceptions.Exit:
            raise
        except click.UsageError:
            raise
        except httpx.ConnectError as exc:
            _msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in _msg:
                raise click.ClickException(
                    f"SSL certificate error connecting to KrewHub.\n"
                    f"  Set KREWCLI_VERIFY_SSL=false to skip verification.\n"
                    f"  Detail: {_msg.splitlines()[-1] if _msg else exc}"
                ) from None
            raise click.ClickException(
                f"Cannot connect to KrewHub: {_msg}"
            ) from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise click.ClickException(
                    "Authentication failed (401). Run 'krewcli login' to refresh your session."
                ) from None
            raise click.ClickException(
                f"KrewHub returned {status}: {exc.response.text[:200]}"
            ) from None
        except httpx.RequestError as exc:
            raise click.ClickException(
                f"Network error: {exc}"
            ) from None


@click.group(cls=_KrewCLI)
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

    from krewcli.auth.token_store import load_token
    jwt_token = load_token()

    ctx.obj["client"] = KrewHubClient(
        settings.krewhub_url,
        settings.api_key,
        jwt_token=jwt_token,
        verify_ssl=settings.verify_ssl,
    )


# ── Register command modules ──

from krewcli.cli.join import register_join_commands, _resolve_mode, _default_model, _run_agent, _run_gateway  # noqa: E402
from krewcli.cli.claim import register_claim_commands, _load_recipe_context  # noqa: E402
from krewcli.cli.tasks import register_task_commands, _run_task_worker, _run_task_worker_once  # noqa: E402
from krewcli.cli.gateway_cmds import register_gateway_commands  # noqa: E402
from krewcli.cli.daemon import register_daemon_commands  # noqa: E402
from krewcli.cli_onboard import register_onboard_command  # noqa: E402
from krewcli.cli_wallet import register_wallet_commands  # noqa: E402

register_join_commands(main)
register_claim_commands(main)
register_task_commands(main)
register_gateway_commands(main)
register_daemon_commands(main)
register_onboard_command(main)
register_wallet_commands(main)

from krewcli.presence.heartbeat import HeartbeatLoop  # noqa: E402, F401


def _attach_compat_attrs(command: click.Command, **attrs: object) -> click.Command:
    for name, value in attrs.items():
        setattr(command, name, value)
    return command


# Backward-compat surface for tests that poke at cli.join, cli.claim, etc.
join = _attach_compat_attrs(
    main.commands["join"],
    _resolve_mode=_resolve_mode,
    _default_model=_default_model,
    _run_agent=_run_agent,
    _run_gateway=_run_gateway,
    HeartbeatLoop=HeartbeatLoop,
    KrewHubClient=KrewHubClient,
    os=os,
)
start = _attach_compat_attrs(main.commands["start"], os=os)
claim = _attach_compat_attrs(
    main.commands["claim"],
    _load_recipe_context=_load_recipe_context,
    HeartbeatLoop=HeartbeatLoop,
    os=os,
)
list_tasks = main.commands["list-tasks"]
milestone = main.commands["milestone"]
status = main.commands["status"]
repo_diagram = main.commands["repo-diagram"]

__all__ = [
    "main",
    "_KrewCLI",
    "join",
    "start",
    "claim",
    "list_tasks",
    "milestone",
    "status",
    "repo_diagram",
    "_resolve_mode",
    "_default_model",
    "_run_agent",
    "_run_gateway",
    "_load_recipe_context",
    "_run_task_worker",
    "_run_task_worker_once",
    "_gateway_agent_metadata",
    "HeartbeatLoop",
    "KrewHubClient",
    "get_settings",
    "os",
    "httpx",
]

if __name__ == "__main__":
    main()
