"""``krewcli logout`` — clear keychain + file token."""
from __future__ import annotations

import click

from krewcli.auth import token_store


@click.command("logout")
def logout_cmd() -> None:
    """Clear stored krewauth credentials."""
    token_store.clear_token()
    click.echo("Logged out")


def register(group: click.Group) -> None:
    group.add_command(logout_cmd)
