from __future__ import annotations

import pytest
from pydantic import ValidationError

from krewcli.auth.models import User, TokenPayload, UserCreate


class TestUserCreate:
    def test_valid_user(self):
        user = UserCreate(email="alice@example.com", password="securepass123")
        assert user.email == "alice@example.com"
        assert user.password == "securepass123"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(email="not-an-email", password="securepass123")

    def test_short_password_rejected(self):
        with pytest.raises(ValidationError, match="at least 8 characters"):
            UserCreate(email="alice@example.com", password="short")

    def test_empty_password_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(email="alice@example.com", password="")

    def test_model_is_immutable(self):
        user = UserCreate(email="alice@example.com", password="securepass123")
        with pytest.raises(ValidationError):
            user.email = "bob@example.com"


class TestUser:
    def test_create_user(self):
        user = User(
            id="user_1",
            email="alice@example.com",
            hashed_password="$2b$12$fakehash",
            is_active=True,
        )
        assert user.id == "user_1"
        assert user.email == "alice@example.com"
        assert user.is_active is True

    def test_default_is_active(self):
        user = User(
            id="user_2",
            email="bob@example.com",
            hashed_password="$2b$12$fakehash",
        )
        assert user.is_active is True

    def test_default_roles_empty(self):
        user = User(
            id="user_3",
            email="carol@example.com",
            hashed_password="$2b$12$fakehash",
        )
        assert user.roles == ()

    def test_user_with_roles(self):
        user = User(
            id="user_4",
            email="dave@example.com",
            hashed_password="$2b$12$fakehash",
            roles=("admin", "editor"),
        )
        assert "admin" in user.roles
        assert "editor" in user.roles

    def test_model_is_immutable(self):
        user = User(
            id="user_5",
            email="eve@example.com",
            hashed_password="$2b$12$fakehash",
        )
        with pytest.raises(ValidationError):
            user.email = "mallory@example.com"

    def test_password_not_in_dict_output(self):
        user = User(
            id="user_6",
            email="frank@example.com",
            hashed_password="$2b$12$fakehash",
        )
        user_dict = user.to_safe_dict()
        assert "hashed_password" not in user_dict
        assert "email" in user_dict
        assert "id" in user_dict


class TestTokenPayload:
    def test_create_payload(self):
        payload = TokenPayload(
            user_id="user_1",
            exp=1700000000.0,
            iat=1699999000.0,
        )
        assert payload.user_id == "user_1"
        assert payload.exp == 1700000000.0

    def test_default_extra_claims_empty(self):
        payload = TokenPayload(
            user_id="user_1",
            exp=1700000000.0,
            iat=1699999000.0,
        )
        assert payload.extra_claims == ()

    def test_with_extra_claims(self):
        payload = TokenPayload(
            user_id="user_1",
            exp=1700000000.0,
            iat=1699999000.0,
            extra_claims=(("role", "admin"), ("org_id", "org_1")),
        )
        claims = dict(payload.extra_claims)
        assert claims["role"] == "admin"
        assert claims["org_id"] == "org_1"

    def test_model_is_immutable(self):
        payload = TokenPayload(
            user_id="user_1",
            exp=1700000000.0,
            iat=1699999000.0,
        )
        with pytest.raises(ValidationError):
            payload.user_id = "user_2"

    def test_extra_claims_is_tuple(self):
        payload = TokenPayload(
            user_id="user_1",
            exp=1700000000.0,
            iat=1699999000.0,
            extra_claims=(("key", "value"),),
        )
        assert isinstance(payload.extra_claims, tuple)
