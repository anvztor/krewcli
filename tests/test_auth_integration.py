from __future__ import annotations

import pytest

from krewcli.auth.models import User, UserCreate
from krewcli.auth.password import verify_password
from krewcli.auth.tokens import create_access_token, decode_access_token, TokenError
from krewcli.auth.service import AuthService, AuthError


FAKE_SECRET = "integration-test-secret-key-minimum-32-chars-long"


class TestAuthServiceInit:
    def test_rejects_short_secret(self):
        with pytest.raises(ValueError, match="at least 32"):
            AuthService(jwt_secret="too-short")

    def test_accepts_valid_secret(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        assert service is not None


class TestAuthServiceRegister:
    def test_register_creates_user(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="alice@example.com", password="securepass123")
        user = service.register(user_input)
        assert user.email == "alice@example.com"
        assert user.id
        assert user.is_active is True

    def test_register_hashes_password(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="bob@example.com", password="securepass123")
        user = service.register(user_input)
        assert user.hashed_password != "securepass123"
        assert verify_password("securepass123", user.hashed_password) is True

    def test_register_duplicate_email_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="dup@example.com", password="securepass123")
        service.register(user_input)
        with pytest.raises(AuthError, match="already registered"):
            service.register(user_input)

    def test_register_assigns_unique_ids(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_a = service.register(UserCreate(email="a@example.com", password="password123!"))
        user_b = service.register(UserCreate(email="b@example.com", password="password123!"))
        assert user_a.id != user_b.id

    def test_register_user_id_uses_full_uuid(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user = service.register(UserCreate(email="uuid@example.com", password="password123!"))
        # user_ prefix + 32 hex chars
        assert user.id.startswith("user_")
        assert len(user.id) == 5 + 32


class TestAuthServiceLogin:
    def _make_service_with_user(self) -> tuple[AuthService, User]:
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="alice@example.com", password="securepass123")
        user = service.register(user_input)
        return service, user

    def test_login_returns_token(self):
        service, _ = self._make_service_with_user()
        token = service.login("alice@example.com", "securepass123")
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    def test_login_token_decodes_to_user(self):
        service, user = self._make_service_with_user()
        token = service.login("alice@example.com", "securepass123")
        payload = decode_access_token(token, secret=FAKE_SECRET)
        assert payload.user_id == user.id

    def test_login_wrong_password_raises(self):
        service, _ = self._make_service_with_user()
        with pytest.raises(AuthError, match="Invalid credentials"):
            service.login("alice@example.com", "wrongpassword")

    def test_login_unknown_email_raises(self):
        service, _ = self._make_service_with_user()
        with pytest.raises(AuthError, match="Invalid credentials"):
            service.login("nobody@example.com", "securepass123")

    def test_login_inactive_user_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="inactive@example.com", password="securepass123")
        user = service.register(user_input)
        service.deactivate_user(user.id)
        with pytest.raises(AuthError, match="Account is deactivated"):
            service.login("inactive@example.com", "securepass123")


class TestAuthServiceAuthenticate:
    def test_authenticate_valid_token(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="alice@example.com", password="securepass123")
        user = service.register(user_input)
        token = service.login("alice@example.com", "securepass123")

        authenticated_user = service.authenticate(token)
        assert authenticated_user.id == user.id
        assert authenticated_user.email == "alice@example.com"

    def test_authenticate_expired_token_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user_input = UserCreate(email="alice@example.com", password="securepass123")
        service.register(user_input)
        expired_token = create_access_token(
            user_id="user_1", secret=FAKE_SECRET, expiry_minutes=-1
        )
        with pytest.raises(AuthError, match="expired"):
            service.authenticate(expired_token)

    def test_authenticate_invalid_token_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        with pytest.raises(AuthError, match="Invalid"):
            service.authenticate("garbage.token.here")

    def test_authenticate_unknown_user_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        token = create_access_token(
            user_id="nonexistent_user", secret=FAKE_SECRET
        )
        with pytest.raises(AuthError, match="User not found"):
            service.authenticate(token)

    def test_authenticate_deactivated_user_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user = service.register(UserCreate(email="deact@example.com", password="securepass123"))
        token = service.login("deact@example.com", "securepass123")
        service.deactivate_user(user.id)
        with pytest.raises(AuthError, match="deactivated"):
            service.authenticate(token)


class TestAuthServiceDeactivate:
    def test_deactivate_unknown_user_raises(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        with pytest.raises(AuthError, match="User not found"):
            service.deactivate_user("nonexistent_id")

    def test_deactivate_sets_inactive(self):
        service = AuthService(jwt_secret=FAKE_SECRET)
        user = service.register(UserCreate(email="d@example.com", password="password123!"))
        deactivated = service.deactivate_user(user.id)
        assert deactivated.is_active is False
        assert deactivated.id == user.id


class TestFullAuthFlow:
    """End-to-end flow: register -> login -> authenticate -> deactivate."""

    def test_full_lifecycle(self):
        service = AuthService(jwt_secret=FAKE_SECRET)

        # Register
        user = service.register(
            UserCreate(email="lifecycle@example.com", password="lifecycle_pass_123")
        )
        assert user.is_active is True

        # Login
        token = service.login("lifecycle@example.com", "lifecycle_pass_123")
        assert token

        # Authenticate with token
        authed_user = service.authenticate(token)
        assert authed_user.id == user.id

        # Deactivate
        service.deactivate_user(user.id)

        # Login should fail after deactivation
        with pytest.raises(AuthError, match="deactivated"):
            service.login("lifecycle@example.com", "lifecycle_pass_123")

        # Token-based auth should also fail after deactivation
        with pytest.raises(AuthError, match="deactivated"):
            service.authenticate(token)

    def test_multiple_users_isolated(self):
        service = AuthService(jwt_secret=FAKE_SECRET)

        user_a = service.register(
            UserCreate(email="a@example.com", password="password_a_123")
        )
        user_b = service.register(
            UserCreate(email="b@example.com", password="password_b_123")
        )

        token_a = service.login("a@example.com", "password_a_123")
        token_b = service.login("b@example.com", "password_b_123")

        assert service.authenticate(token_a).id == user_a.id
        assert service.authenticate(token_b).id == user_b.id

        # Cross-check: A's password doesn't work for B
        with pytest.raises(AuthError, match="Invalid credentials"):
            service.login("b@example.com", "password_a_123")
