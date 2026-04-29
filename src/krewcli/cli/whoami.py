"""``krewcli whoami`` — print stored account info."""
from __future__ import annotations

import click
import jwt

from krewcli.auth import token_store


@click.command("whoami")
def whoami_cmd() -> None:
    """Decode the stored JWT and print account_id + expiry."""
    rec = token_store.load_record()
    if rec:
        token = rec.get("token")
    else:
        token = token_store.load_token()
        rec = None

    if not token:
        click.echo("Not logged in")
        return

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(f"Stored token is malformed: {exc}")
        return

    sub = payload.get("sub", "?")
    method = payload.get("auth_method") or payload.get("method") or "?"
    exp = payload.get("exp", "?")
    click.echo(f"Account: {sub}  Method: {method}  Expires: {exp}")


def register(group: click.Group) -> None:
    group.add_command(whoami_cmd)
