"""Tests for file-based JWT token persistence."""

from __future__ import annotations

import os
import stat

from krewcli.auth.token_store import save_token, load_token, clear_token


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
