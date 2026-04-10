"""Wallet key management for CLI SIWE authentication.

Stores a private key at ~/.krewcli/wallet with restrictive permissions.
The key is used to sign SIWE messages for krewhub authentication.

Both human and agent operations use the same wallet — the X-Acting-As
header switches between human and agent modes.
"""

from __future__ import annotations

import os
from pathlib import Path

from eth_account import Account

_DEFAULT_DIR = Path.home() / ".krewcli"
_WALLET_FILE = "wallet"


def _resolve_path(directory: Path | None = None) -> Path:
    return (directory or _DEFAULT_DIR) / _WALLET_FILE


def save_private_key(key_hex: str, directory: Path | None = None) -> Path:
    """Write private key to disk with restrictive permissions (0600)."""
    dir_path = directory or _DEFAULT_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    os.chmod(dir_path, 0o700)

    path = dir_path / _WALLET_FILE
    # Store as hex without 0x prefix
    clean_key = key_hex.removeprefix("0x")
    path.write_text(clean_key, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_private_key(directory: Path | None = None) -> str | None:
    """Read the stored private key, or return None if not found."""
    path = _resolve_path(directory)
    if not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    return f"0x{key}" if not key.startswith("0x") else key


def get_wallet_address(directory: Path | None = None) -> str | None:
    """Derive the wallet address from the stored private key."""
    key = load_private_key(directory)
    if key is None:
        return None
    acct = Account.from_key(key)
    return acct.address


def generate_wallet(directory: Path | None = None) -> tuple[str, str]:
    """Generate a new wallet and save it. Returns (address, key_hex)."""
    acct = Account.create()
    key_hex = acct.key.hex()
    save_private_key(key_hex, directory)
    return acct.address, key_hex


def clear_wallet(directory: Path | None = None) -> None:
    """Delete the stored wallet key."""
    path = _resolve_path(directory)
    if path.is_file():
        path.unlink()
