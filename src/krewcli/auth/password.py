from __future__ import annotations

import hashlib

import bcrypt


def _prepare_password(plaintext: str) -> bytes:
    """SHA-256 pre-hash to safely handle passwords of any length within bcrypt's 72-byte limit."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest().encode("utf-8")


# Pre-computed dummy hash for constant-time login (timing-attack mitigation).
DUMMY_HASH = bcrypt.hashpw(_prepare_password("dummy-timing-equalization"), bcrypt.gensalt()).decode(
    "utf-8"
)


def hash_password(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("Password cannot be empty")
    return bcrypt.hashpw(_prepare_password(plaintext), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    if not plaintext or not hashed:
        return False
    return bcrypt.checkpw(_prepare_password(plaintext), hashed.encode("utf-8"))
