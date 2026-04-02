from __future__ import annotations

import time

import pytest

from krewcli.auth.tokens import create_access_token, decode_access_token, TokenError


FAKE_SECRET = "test-secret-key-for-jwt-testing-minimum-32-chars"


class TestCreateAccessToken:
    def test_returns_string(self):
        token = create_access_token(
            user_id="user_1", secret=FAKE_SECRET
        )
        assert isinstance(token, str)

    def test_token_has_three_parts(self):
        token = create_access_token(
            user_id="user_1", secret=FAKE_SECRET
        )
        parts = token.split(".")
        assert len(parts) == 3

    def test_different_users_get_different_tokens(self):
        token_a = create_access_token(user_id="user_1", secret=FAKE_SECRET)
        token_b = create_access_token(user_id="user_2", secret=FAKE_SECRET)
        assert token_a != token_b

    def test_custom_expiry_minutes(self):
        token = create_access_token(
            user_id="user_1", secret=FAKE_SECRET, expiry_minutes=60
        )
        payload = decode_access_token(token, secret=FAKE_SECRET)
        assert payload.user_id == "user_1"

    def test_extra_claims_included(self):
        token = create_access_token(
            user_id="user_1",
            secret=FAKE_SECRET,
            extra_claims={"role": "admin"},
        )
        payload = decode_access_token(token, secret=FAKE_SECRET)
        claims = dict(payload.extra_claims)
        assert claims["role"] == "admin"


class TestDecodeAccessToken:
    def test_roundtrip(self):
        token = create_access_token(user_id="user_42", secret=FAKE_SECRET)
        payload = decode_access_token(token, secret=FAKE_SECRET)
        assert payload.user_id == "user_42"

    def test_payload_has_expiry(self):
        token = create_access_token(user_id="user_1", secret=FAKE_SECRET)
        payload = decode_access_token(token, secret=FAKE_SECRET)
        assert payload.exp > time.time()

    def test_payload_has_issued_at(self):
        before = time.time()
        token = create_access_token(user_id="user_1", secret=FAKE_SECRET)
        payload = decode_access_token(token, secret=FAKE_SECRET)
        assert payload.iat >= before

    def test_expired_token_raises(self):
        token = create_access_token(
            user_id="user_1", secret=FAKE_SECRET, expiry_minutes=-1
        )
        with pytest.raises(TokenError, match="expired"):
            decode_access_token(token, secret=FAKE_SECRET)

    def test_wrong_secret_raises(self):
        token = create_access_token(user_id="user_1", secret=FAKE_SECRET)
        with pytest.raises(TokenError, match="Invalid token"):
            decode_access_token(token, secret="wrong-secret-that-is-at-least-32-chars")

    def test_malformed_token_raises(self):
        with pytest.raises(TokenError, match="Invalid token"):
            decode_access_token("not.a.jwt", secret=FAKE_SECRET)

    def test_tampered_token_raises(self):
        token = create_access_token(user_id="user_1", secret=FAKE_SECRET)
        parts = token.split(".")
        tampered = parts[0] + "." + parts[1] + ".tampered_signature"
        with pytest.raises(TokenError, match="Invalid token"):
            decode_access_token(tampered, secret=FAKE_SECRET)

    def test_empty_token_raises(self):
        with pytest.raises(TokenError):
            decode_access_token("", secret=FAKE_SECRET)

    def test_extra_claims_are_immutable_tuples(self):
        token = create_access_token(
            user_id="user_1",
            secret=FAKE_SECRET,
            extra_claims={"role": "admin", "org": "acme"},
        )
        payload = decode_access_token(token, secret=FAKE_SECRET)
        assert isinstance(payload.extra_claims, tuple)
        claims = dict(payload.extra_claims)
        assert claims["role"] == "admin"
        assert claims["org"] == "acme"
