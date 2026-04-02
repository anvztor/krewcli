from __future__ import annotations

import time
from typing import Any

import jwt

from krewcli.auth.models import TokenPayload


class TokenError(Exception):
    """Raised when token creation or validation fails."""


def create_access_token(
    user_id: str,
    secret: str,
    expiry_minutes: int = 30,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = time.time()
    payload: dict[str, Any] = {
        "sub": user_id,
        "iat": now,
        "exp": now + (expiry_minutes * 60),
    }
    if extra_claims:
        payload["extra"] = extra_claims
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> TokenPayload:
    try:
        data = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise TokenError("Token has expired")
    except jwt.InvalidTokenError:
        raise TokenError("Invalid token")

    raw_extra = data.get("extra", {})
    extra_claims = tuple(raw_extra.items()) if isinstance(raw_extra, dict) else ()

    return TokenPayload(
        user_id=data["sub"],
        exp=data["exp"],
        iat=data["iat"],
        extra_claims=extra_claims,
    )
