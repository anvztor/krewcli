"""Tests for gateway.py helper functions introduced by the refactor.

Covers _gateway_agent_metadata, load_recipe_context, build_auth_service,
_get_owner_label, and _make_agent_id — pure functions that can be tested
without starting a server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from krewcli.gateway_helpers import (
    _gateway_agent_metadata,
    _get_owner_label,
    _make_agent_id,
    build_auth_service,
    load_recipe_context,
)


class TestGatewayAgentMetadata:
    def test_returns_display_name_and_capabilities_for_known_agent(self):
        display_name, caps = _gateway_agent_metadata("claude")
        assert display_name == "Claude Agent"
        assert isinstance(caps, list)
        assert "claim" in caps

    def test_returns_raw_name_for_unknown_agent(self):
        display_name, caps = _gateway_agent_metadata("nonexistent_agent")
        assert display_name == "nonexistent_agent"
        assert caps == []

    def test_all_registered_agents_return_capabilities(self):
        from krewcli.agents.registry import AGENT_REGISTRY
        for name in AGENT_REGISTRY:
            display_name, caps = _gateway_agent_metadata(name)
            assert isinstance(display_name, str)
            assert len(caps) > 0


class TestMakeAgentId:
    def test_format(self):
        assert _make_agent_id("claude", "alice") == "claude@alice"

    def test_special_characters_preserved(self):
        assert _make_agent_id("bub", "user.name") == "bub@user.name"


class TestGetOwnerLabel:
    def test_returns_local_when_no_token(self):
        with patch("krewcli.auth.token_store.load_token", return_value=None):
            assert _get_owner_label() == "local"

    def test_returns_username_from_jwt(self):
        import jwt as pyjwt
        token = pyjwt.encode({"username": "alice", "sub": "sub_id"}, "secret", algorithm="HS256")
        with patch("krewcli.auth.token_store.load_token", return_value=token):
            assert _get_owner_label() == "alice"

    def test_falls_back_to_sub_when_no_username(self):
        import jwt as pyjwt
        token = pyjwt.encode({"sub": "wallet_0x123"}, "secret", algorithm="HS256")
        with patch("krewcli.auth.token_store.load_token", return_value=token):
            assert _get_owner_label() == "wallet_0x123"

    def test_returns_local_on_decode_error(self):
        with patch("krewcli.auth.token_store.load_token", return_value="not.a.jwt"):
            assert _get_owner_label() == "local"


class TestLoadRecipeContext:
    @pytest.mark.asyncio
    async def test_returns_repo_url_and_branch(self):
        client = AsyncMock()
        client.get_recipe = AsyncMock(return_value={
            "recipe": {
                "repo_url": "git@github.com:org/repo.git",
                "default_branch": "develop",
            }
        })
        repo_url, branch = await load_recipe_context(client, "rec_42")
        assert repo_url == "git@github.com:org/repo.git"
        assert branch == "develop"
        client.get_recipe.assert_awaited_once_with("rec_42")

    @pytest.mark.asyncio
    async def test_defaults_branch_to_main(self):
        client = AsyncMock()
        client.get_recipe = AsyncMock(return_value={
            "recipe": {"repo_url": "https://example.com/repo.git"}
        })
        repo_url, branch = await load_recipe_context(client, "rec_1")
        assert branch == "main"

    @pytest.mark.asyncio
    async def test_empty_recipe_returns_empty_url(self):
        client = AsyncMock()
        client.get_recipe = AsyncMock(return_value={"recipe": {}})
        repo_url, branch = await load_recipe_context(client, "rec_empty")
        assert repo_url == ""
        assert branch == "main"


class TestBuildAuthService:
    def test_returns_none_when_no_secret(self):
        settings = Mock()
        settings.jwt_secret = ""
        assert build_auth_service(settings) is None

    def test_returns_none_when_secret_too_short(self):
        settings = Mock()
        settings.jwt_secret = "short"
        assert build_auth_service(settings) is None

    def test_returns_auth_service_when_secret_valid(self):
        from krewcli.auth.service import AuthService
        settings = Mock()
        settings.jwt_secret = "a" * 32
        settings.token_expiry_minutes = 60
        result = build_auth_service(settings)
        assert isinstance(result, AuthService)

    def test_returns_none_when_secret_exactly_31_chars(self):
        settings = Mock()
        settings.jwt_secret = "a" * 31
        assert build_auth_service(settings) is None

    def test_returns_service_when_secret_exactly_32_chars(self):
        settings = Mock()
        settings.jwt_secret = "a" * 32
        settings.token_expiry_minutes = 30
        result = build_auth_service(settings)
        assert result is not None
