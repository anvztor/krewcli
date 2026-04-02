from __future__ import annotations

import pytest

from krewcli.auth.password import hash_password, verify_password


class TestHashPassword:
    def test_returns_string(self):
        hashed = hash_password("mysecretpassword")
        assert isinstance(hashed, str)

    def test_hash_differs_from_plaintext(self):
        plaintext = "mysecretpassword"
        hashed = hash_password(plaintext)
        assert hashed != plaintext

    def test_different_calls_produce_different_hashes(self):
        hashed_a = hash_password("same_password")
        hashed_b = hash_password("same_password")
        assert hashed_a != hashed_b

    def test_hash_starts_with_bcrypt_prefix(self):
        hashed = hash_password("test123")
        assert hashed.startswith("$2b$")

    def test_empty_password_raises(self):
        with pytest.raises(ValueError, match="Password cannot be empty"):
            hash_password("")


class TestVerifyPassword:
    def test_correct_password_returns_true(self):
        hashed = hash_password("correct_password")
        assert verify_password("correct_password", hashed) is True

    def test_wrong_password_returns_false(self):
        hashed = hash_password("correct_password")
        assert verify_password("wrong_password", hashed) is False

    def test_case_sensitive(self):
        hashed = hash_password("Password")
        assert verify_password("password", hashed) is False
        assert verify_password("PASSWORD", hashed) is False

    def test_unicode_password(self):
        hashed = hash_password("pässwörd_日本語")
        assert verify_password("pässwörd_日本語", hashed) is True
        assert verify_password("password", hashed) is False

    def test_long_password(self):
        long_pw = "a" * 200
        hashed = hash_password(long_pw)
        assert verify_password(long_pw, hashed) is True

    def test_special_characters(self):
        special_pw = "p@$$w0rd!#%^&*()_+-=[]{}|;':\",./<>?"
        hashed = hash_password(special_pw)
        assert verify_password(special_pw, hashed) is True

    def test_empty_plaintext_returns_false(self):
        hashed = hash_password("validpassword")
        assert verify_password("", hashed) is False

    def test_empty_hash_returns_false(self):
        assert verify_password("somepassword", "") is False
