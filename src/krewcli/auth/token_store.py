"""File-based JWT token persistence for the CLI."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DIR = Path.home() / ".krewcli"
_TOKEN_FILE = "token"


def _resolve_path(directory: Path | None = None) -> Path:
    return (directory or _DEFAULT_DIR) / _TOKEN_FILE


def save_token(token: str, directory: Path | None = None) -> Path:
    """Write *token* to disk with restrictive permissions. Returns the file path."""
    dir_path = directory or _DEFAULT_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    os.chmod(dir_path, 0o700)

    path = dir_path / _TOKEN_FILE
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_token(directory: Path | None = None) -> str | None:
    """Read the stored token, or return ``None`` if no token file exists."""
    path = _resolve_path(directory)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def clear_token(directory: Path | None = None) -> None:
    """Delete the stored token file if it exists."""
    path = _resolve_path(directory)
    if path.is_file():
        path.unlink()
