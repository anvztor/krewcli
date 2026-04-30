"""``krewcli login`` — start the device authorization flow against krewauth."""
from __future__ import annotations

import asyncio

import click

from krewcli.auth import token_store
from krewcli.auth import device_flow
from krewcli.config import get_settings


async def _login() -> None:
    settings = get_settings()
    dc = await device_flow.request(settings.krew_auth_url)
    device_flow.display_code(dc)
    tok = await device_flow.poll(settings.krew_auth_url, dc.device_code)
    token_store.save_record({
        "token": tok.token,
        "account_id": tok.account_id,
        "expires_at": tok.expires_at,
    })
    # Also write the legacy raw-token file so existing daemon code keeps working.
    token_store.save_token(tok.token)
    click.echo(f"Logged in as {tok.account_id}")


@click.command("login")
def login_cmd() -> None:
    """Start passkey/device flow login against krewauth."""
    asyncio.run(_login())


def register(group: click.Group) -> None:
    group.add_command(login_cmd)
