from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, field_validator


class UserCreate(BaseModel):
    """Input model for user registration."""

    model_config = {"frozen": True}

    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class User(BaseModel):
    """Stored user record. The hashed_password is never exposed via to_safe_dict."""

    model_config = {"frozen": True}

    id: str
    email: EmailStr
    hashed_password: str
    is_active: bool = True
    roles: tuple[str, ...] = ()

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "is_active": self.is_active,
            "roles": list(self.roles),
        }


class TokenPayload(BaseModel):
    """Decoded JWT token payload."""

    model_config = {"frozen": True}

    user_id: str
    exp: float
    iat: float
    extra_claims: tuple[tuple[str, Any], ...] = ()
