"""Token storage: keyring with file fallback.

Two API surfaces:

1. Legacy raw-token (used by daemon, gateway, CLI client construction):
   - ``save_token(token, directory=None)``
   - ``load_token(directory=None) -> str | None``
   - ``clear_token(directory=None)``

2. Record-based (used by ``krewcli login`` to store account/expiry alongside
   the token):
   - ``save_record({token, account_id, expires_at})``
   - ``load_record() -> dict | None``
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT_DIR = Path.home() / ".krewcli"
_TOKEN_FILE = "token"
_RECORD_FILE = "token.json"

_KEYRING_SERVICE = "krewcli"
_KEYRING_USER = "default"


def _resolve_path(directory: Path | None = None) -> Path:
    return (directory or _DEFAULT_DIR) / _TOKEN_FILE


def _resolve_record_path(directory: Path | None = None) -> Path:
    return (directory or _DEFAULT_DIR) / _RECORD_FILE


def _try_keyring():
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Legacy raw-token API
# ---------------------------------------------------------------------------


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
    # First try the record (preferred storage going forward)
    record = load_record(directory)
    if record:
        token = record.get("token")
        if token:
            return token
    path = _resolve_path(directory)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def clear_token(directory: Path | None = None) -> None:
    """Delete the stored token file if it exists."""
    path = _resolve_path(directory)
    if path.is_file():
        path.unlink()
    rec = _resolve_record_path(directory)
    if rec.is_file():
        rec.unlink()
    kr = _try_keyring()
    if kr:
        try:
            kr.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Record-based API (login command stores account + expiry alongside token)
# ---------------------------------------------------------------------------


def save_record(
    record: dict,
    directory: Path | None = None,
) -> None:
    """Persist a {token, account_id, expires_at} record.

    Tries the OS keyring first; falls back to a 0600 file at
    ``~/.krewcli/token.json`` (or *directory*).
    """
    payload = json.dumps(record)
    kr = _try_keyring()
    if kr:
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_USER, payload)
            return
        except Exception:
            # Keyring backend missing on this host — fall through to file.
            pass
    dir_path = directory or _DEFAULT_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    os.chmod(dir_path, 0o700)
    path = dir_path / _RECORD_FILE
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def load_record(directory: Path | None = None) -> dict | None:
    """Read a stored {token, account_id, expires_at} record, if any."""
    kr = _try_keyring()
    if kr:
        try:
            v = kr.get_password(_KEYRING_SERVICE, _KEYRING_USER)
            if v:
                return json.loads(v)
        except Exception:
            pass
    path = _resolve_record_path(directory)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None
