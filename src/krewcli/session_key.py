"""Session key management for ERC-4337 smart account agent operations.

A session key is a regular secp256k1 key pair generated locally.
The human approves it on-chain via addSessionKey() on the smart account.
The agent uses it to sign UserOperations within its granted scope.
"""

from __future__ import annotations

import os
from pathlib import Path

from eth_account import Account

_DEFAULT_DIR = Path.home() / ".krewcli"
_SESSION_KEY_FILE = "session_key"


def generate_session_key(directory: Path | None = None) -> tuple[str, str]:
    """Generate a new session key pair. Returns (address, private_key_hex)."""
    acct = Account.create()
    key_hex = acct.key.hex()
    save_session_key(key_hex, directory)
    return acct.address, key_hex


def save_session_key(key_hex: str, directory: Path | None = None) -> Path:
    """Save session key to disk with restrictive permissions."""
    dir_path = directory or _DEFAULT_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    os.chmod(dir_path, 0o700)

    path = dir_path / _SESSION_KEY_FILE
    clean = key_hex.removeprefix("0x")
    path.write_text(clean, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_session_key(directory: Path | None = None) -> str | None:
    """Load session key from disk. Returns hex private key or None."""
    path = (directory or _DEFAULT_DIR) / _SESSION_KEY_FILE
    if not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    return f"0x{key}" if not key.startswith("0x") else key


def get_session_key_address(directory: Path | None = None) -> str | None:
    """Derive the address from the stored session key."""
    key = load_session_key(directory)
    if key is None:
        return None
    return Account.from_key(key).address
