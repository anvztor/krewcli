from __future__ import annotations

import uuid

from krewcli.auth.models import User, UserCreate
from krewcli.auth.password import hash_password, verify_password, DUMMY_HASH
from krewcli.auth.tokens import (
    create_access_token,
    decode_access_token,
    TokenError,
)

_MIN_SECRET_LENGTH = 32


class AuthError(Exception):
    """Raised for authentication/authorization failures."""


class AuthService:
    """In-memory auth service for user registration, login, and token validation."""

    def __init__(self, jwt_secret: str, token_expiry_minutes: int = 30) -> None:
        if len(jwt_secret) < _MIN_SECRET_LENGTH:
            raise ValueError(
                f"jwt_secret must be at least {_MIN_SECRET_LENGTH} characters"
            )
        self._jwt_secret = jwt_secret
        self._token_expiry_minutes = token_expiry_minutes
        self._users_by_id: dict[str, User] = {}
        self._users_by_email: dict[str, User] = {}

    def register(self, user_input: UserCreate) -> User:
        if user_input.email in self._users_by_email:
            raise AuthError("Email is already registered")

        user = User(
            id=f"user_{uuid.uuid4().hex}",
            email=user_input.email,
            hashed_password=hash_password(user_input.password),
        )
        self._users_by_id[user.id] = user
        self._users_by_email[user.email] = user
        return user

    def login(self, email: str, password: str) -> str:
        user = self._users_by_email.get(email)
        if user is None:
            verify_password(password, DUMMY_HASH)
            raise AuthError("Invalid credentials")

        if not verify_password(password, user.hashed_password):
            raise AuthError("Invalid credentials")

        if not user.is_active:
            raise AuthError("Account is deactivated")

        return create_access_token(
            user_id=user.id,
            secret=self._jwt_secret,
            expiry_minutes=self._token_expiry_minutes,
        )

    def authenticate(self, token: str) -> User:
        try:
            payload = decode_access_token(token, secret=self._jwt_secret)
        except TokenError as exc:
            raise AuthError(str(exc)) from exc

        user = self._users_by_id.get(payload.user_id)
        if user is None:
            raise AuthError("User not found")
        if not user.is_active:
            raise AuthError("Account is deactivated")
        return user

    def deactivate_user(self, user_id: str) -> User:
        user = self._users_by_id.get(user_id)
        if user is None:
            raise AuthError("User not found")

        deactivated = User(
            id=user.id,
            email=user.email,
            hashed_password=user.hashed_password,
            is_active=False,
            roles=user.roles,
        )
        self._users_by_id[user_id] = deactivated
        self._users_by_email[user.email] = deactivated
        return deactivated
