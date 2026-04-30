"""Wallet, session-key, and login CLI commands — extracted from cli.py."""

from __future__ import annotations

import click


def register_wallet_commands(main: click.Group) -> None:
    """Register wallet, session-key, and login commands on the CLI group."""

    @main.group("wallet")
    def wallet_group():
        """Manage wallet identity for SIWE authentication."""
        pass

    @wallet_group.command("create")
    def wallet_create():
        """Generate a new Ethereum wallet and save to ~/.krewcli/wallet."""
        from krewcli.auth.wallet import generate_wallet

        address, key_hex = generate_wallet()
        click.echo(f"Wallet created: {address}")
        click.echo(f"Private key saved to ~/.krewcli/wallet")
        click.echo(f"Back up this key! If lost, you lose access to this identity.")

    @wallet_group.command("import")
    @click.argument("private_key")
    def wallet_import(private_key):
        """Import an existing private key. Usage: krewcli wallet import 0x..."""
        from eth_account import Account
        from krewcli.auth.wallet import save_private_key

        try:
            acct = Account.from_key(private_key)
        except Exception:
            click.echo("Error: Invalid private key.", err=True)
            raise SystemExit(1)

        save_private_key(private_key)
        click.echo(f"Wallet imported: {acct.address}")
        click.echo(f"Saved to ~/.krewcli/wallet")

    @wallet_group.command("address")
    def wallet_address():
        """Show the current wallet address."""
        from krewcli.auth.wallet import get_wallet_address

        addr = get_wallet_address()
        if addr is None:
            click.echo("No wallet found. Run 'krewcli wallet create' first.", err=True)
            raise SystemExit(1)
        click.echo(addr)

    @main.group("session-key")
    def session_key_group():
        """Manage session keys for ERC-4337 smart account operations."""
        pass

    @session_key_group.command("create")
    def session_key_create():
        """Generate a new session key for agent operations."""
        from krewcli.session_key import generate_session_key

        address, _ = generate_session_key()
        click.echo(f"Session key created: {address}")
        click.echo("Saved to ~/.krewcli/session_key")
        click.echo("Request approval: human must call addSessionKey() on the smart account")

    @session_key_group.command("address")
    def session_key_address():
        """Show the current session key address."""
        from krewcli.session_key import get_session_key_address

        addr = get_session_key_address()
        if addr is None:
            click.echo("No session key. Run 'krewcli session-key create'.", err=True)
            raise SystemExit(1)
        click.echo(addr)

    # Note: ``krewcli login`` lives in krewcli.cli.login as of Track A1.
    # The previous synchronous implementation here was superseded by the
    # device_flow-driven async version that pairs via cookrew.
