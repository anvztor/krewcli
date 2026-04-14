"""Wallet, session-key, and login CLI commands — extracted from cli.py."""

from __future__ import annotations

import click
import httpx


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

    @main.command("login")
    @click.pass_context
    def login(ctx):
        """Log in via krewauth device authorization (approve in browser).

        Opens the krewauth login page. Authenticate with passkey or wallet.
        No private key needed on this machine.
        """
        import time
        from krewcli.auth.token_store import save_token

        settings = ctx.obj["settings"]
        auth_url = settings.krew_auth_url

        try:
            verify = getattr(settings, "verify_ssl", True)
            with httpx.Client(timeout=10, verify=verify) as http:
                resp = http.post(f"{auth_url}/auth/device/request")
                resp.raise_for_status()
                data = resp.json()
                device_code = data["device_code"]
                user_code = data["user_code"]
                verification_uri = f"{auth_url}/auth/login?device_code={user_code}"
                expires_in = data["expires_in"]

                click.echo()
                click.echo(f"  Open: {verification_uri}")
                click.echo(f"  Code: {user_code}")
                click.echo()
                click.echo(f"  Waiting for approval (expires in {expires_in // 60} min)...")

                import webbrowser
                webbrowser.open(verification_uri)

                poll_interval = 3
                elapsed = 0
                while elapsed < expires_in:
                    time.sleep(poll_interval)
                    elapsed += poll_interval

                    resp = http.post(
                        f"{auth_url}/auth/device/token",
                        json={"device_code": device_code},
                    )
                    if resp.status_code == 404:
                        click.echo("Error: Code expired.", err=True)
                        raise SystemExit(1)

                    resp.raise_for_status()
                    result = resp.json()

                    if result["status"] == "approved":
                        save_token(result["token"])
                        from krewcli.gateway import _get_owner_label
                        _label = _get_owner_label()
                        click.echo(f"\n  Logged in as @{_label}")
                        click.echo(f"  Account: {result.get('account_id', 'unknown')}")
                        if result.get("wallet_address"):
                            click.echo(f"  Wallet: {result['wallet_address']}")
                        click.echo(f"  Session expires: {result['expires_at']}")
                        click.echo(f"  JWT saved to ~/.krewcli/token")
                        return

                click.echo("Error: Timed out waiting for approval.", err=True)
                raise SystemExit(1)

        except httpx.ConnectError:
            click.echo(f"Error: Could not connect to krewauth at {auth_url}", err=True)
            raise SystemExit(1)
