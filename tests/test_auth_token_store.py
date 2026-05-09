"""Tests for file-based JWT token persistence."""

from __future__ import annotations

import os
import stat

import jwt as _pyjwt

from krewcli.auth.token_store import (
    account_id_from_token,
    save_token,
    load_token,
    clear_token,
)


class TestSaveToken:
    def test_creates_file(self, tmp_path):
        path = save_token("my-jwt-token", directory=tmp_path)
        assert path.exists()
        assert path.read_text() == "my-jwt-token"

    def test_file_permissions(self, tmp_path):
        path = save_token("tok", directory=tmp_path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_directory_permissions(self, tmp_path):
        sub = tmp_path / "nested"
        save_token("tok", directory=sub)
        mode = stat.S_IMODE(os.stat(sub).st_mode)
        assert mode == 0o700

    def test_overwrites_existing(self, tmp_path):
        save_token("first", directory=tmp_path)
        save_token("second", directory=tmp_path)
        assert load_token(directory=tmp_path) == "second"


class TestLoadToken:
    def test_returns_token(self, tmp_path):
        save_token("stored", directory=tmp_path)
        assert load_token(directory=tmp_path) == "stored"

    def test_returns_none_when_missing(self, tmp_path):
        assert load_token(directory=tmp_path) is None

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / "token").write_text("  tok  \n")
        assert load_token(directory=tmp_path) == "tok"


class TestClearToken:
    def test_removes_file(self, tmp_path):
        save_token("tok", directory=tmp_path)
        clear_token(directory=tmp_path)
        assert load_token(directory=tmp_path) is None

    def test_no_error_when_missing(self, tmp_path):
        clear_token(directory=tmp_path)  # should not raise


class TestAccountIdFromToken:
    """Daemon runtime registration depends on extracting account_id from
    the JWT itself (sub claim). Reading it from a sidecar record is fragile
    — keyring/file can drift out of sync with the raw token, and a miss
    silently disables runtime registration so cookrew-beta shows NO AGENTS.
    """

    @staticmethod
    def _make(payload: dict) -> str:
        return _pyjwt.encode(payload, "irrelevant-for-no-verify", algorithm="HS256")

    def test_extracts_sub(self):
        token = self._make({"sub": "acc_abc123", "iss": "krewauth"})
        assert account_id_from_token(token) == "acc_abc123"

    def test_returns_none_for_empty(self):
        assert account_id_from_token(None) is None
        assert account_id_from_token("") is None

    def test_returns_none_for_invalid_jwt(self):
        assert account_id_from_token("not-a-jwt") is None
        assert account_id_from_token("a.b.c") is None

    def test_returns_none_when_sub_missing(self):
        token = self._make({"iss": "krewauth"})
        assert account_id_from_token(token) is None

    def test_does_not_require_signature_verification(self):
        # krewcli is a relying party — krewhub verifies the signature.
        # The daemon just reads its own token to know who it is.
        token = self._make({"sub": "acc_xyz"})
        # Tampered signature: base64-decoded payload is still readable.
        head, body, _ = token.split(".")
        tampered = f"{head}.{body}.deadbeef"
        assert account_id_from_token(tampered) == "acc_xyz"
