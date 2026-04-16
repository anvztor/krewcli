"""Unit tests for krewcli.config — Settings model and get_settings cache.

Covers:
- Default values for every Settings field
- ``KREWCLI_`` env-var prefix overrides (incl. type coercion)
- ``get_settings`` lru_cache returns the same instance
- ``Settings.model_copy(update=...)`` immutability used by the join command

These tests scrub ambient ``KREWCLI_*`` env vars so they are stable on
developer machines that have prod URLs exported.
"""

from __future__ import annotations

import os

import pytest

from krewcli import config as config_module
from krewcli.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_krewcli_env(monkeypatch):
    """Strip ambient KREWCLI_* vars before each test so defaults are observable."""
    for key in [k for k in os.environ if k.startswith("KREWCLI_")]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    def test_default_krewhub_and_auth_urls(self):
        s = Settings()
        assert s.krewhub_url == "http://127.0.0.1:8420"
        assert s.krew_auth_url == "http://127.0.0.1:8421"

    def test_default_api_key(self):
        s = Settings()
        assert s.api_key == "dev-api-key"

    def test_default_agent_host_and_port(self):
        s = Settings()
        assert s.agent_host == "127.0.0.1"
        assert s.agent_port == 9999

    def test_default_intervals(self):
        s = Settings()
        assert s.heartbeat_interval == 15
        assert s.task_poll_interval == 5
        assert s.token_expiry_minutes == 30

    def test_default_recipe_and_cookbook_blank(self):
        s = Settings()
        assert s.default_recipe_id == ""
        assert s.default_cookbook_id == ""

    def test_default_jwt_secret_blank(self):
        s = Settings()
        assert s.jwt_secret == ""

    def test_default_hook_listener_port(self):
        s = Settings()
        assert s.hook_listener_port == 9998

    def test_default_verify_ssl_true(self):
        s = Settings()
        assert s.verify_ssl is True

    def test_default_erc8004_chain_metadata(self):
        s = Settings()
        # GOAT Testnet3 — guard against accidental chain swap.
        assert s.erc8004_chain_id == 48816
        assert s.erc8004_rpc_url == "https://rpc.testnet3.goat.network"
        assert s.erc8004_identity_registry.startswith("0x")
        assert len(s.erc8004_identity_registry) == 42
        assert s.erc8004_reputation_registry.startswith("0x")
        assert len(s.erc8004_reputation_registry) == 42


# ---------------------------------------------------------------------------
# Env-var overrides via KREWCLI_ prefix
# ---------------------------------------------------------------------------


class TestSettingsEnvOverrides:
    def test_string_env_override(self, monkeypatch):
        monkeypatch.setenv("KREWCLI_KREWHUB_URL", "https://prod-hub.example.com")
        monkeypatch.setenv("KREWCLI_API_KEY", "prod-secret")
        s = Settings()
        assert s.krewhub_url == "https://prod-hub.example.com"
        assert s.api_key == "prod-secret"

    def test_int_env_coerced(self, monkeypatch):
        monkeypatch.setenv("KREWCLI_AGENT_PORT", "12345")
        monkeypatch.setenv("KREWCLI_HEARTBEAT_INTERVAL", "60")
        monkeypatch.setenv("KREWCLI_TOKEN_EXPIRY_MINUTES", "1440")
        s = Settings()
        assert s.agent_port == 12345
        assert s.heartbeat_interval == 60
        assert s.token_expiry_minutes == 1440

    def test_bool_env_coerced_false(self, monkeypatch):
        # pydantic-settings coerces "false"/"0" to False
        monkeypatch.setenv("KREWCLI_VERIFY_SSL", "false")
        s = Settings()
        assert s.verify_ssl is False

    def test_bool_env_coerced_true_variants(self, monkeypatch):
        monkeypatch.setenv("KREWCLI_VERIFY_SSL", "true")
        assert Settings().verify_ssl is True
        monkeypatch.setenv("KREWCLI_VERIFY_SSL", "1")
        assert Settings().verify_ssl is True

    def test_default_cookbook_and_recipe_env(self, monkeypatch):
        monkeypatch.setenv("KREWCLI_DEFAULT_COOKBOOK_ID", "cb_prod")
        monkeypatch.setenv("KREWCLI_DEFAULT_RECIPE_ID", "rec_prod")
        s = Settings()
        assert s.default_cookbook_id == "cb_prod"
        assert s.default_recipe_id == "rec_prod"

    def test_erc8004_env_override(self, monkeypatch):
        monkeypatch.setenv("KREWCLI_ERC8004_CHAIN_ID", "1")
        monkeypatch.setenv("KREWCLI_ERC8004_RPC_URL", "https://mainnet.example/rpc")
        monkeypatch.setenv(
            "KREWCLI_ERC8004_IDENTITY_REGISTRY",
            "0x0000000000000000000000000000000000000001",
        )
        s = Settings()
        assert s.erc8004_chain_id == 1
        assert s.erc8004_rpc_url == "https://mainnet.example/rpc"
        assert s.erc8004_identity_registry == "0x0000000000000000000000000000000000000001"

    def test_unprefixed_env_is_ignored(self, monkeypatch):
        # The model_config uses env_prefix="KREWCLI_", so naked vars must NOT leak.
        monkeypatch.setenv("KREWHUB_URL", "https://leaked.example.com")
        monkeypatch.setenv("API_KEY", "leaked-secret")
        s = Settings()
        assert s.krewhub_url == "http://127.0.0.1:8420"
        assert s.api_key == "dev-api-key"


# ---------------------------------------------------------------------------
# get_settings caching
# ---------------------------------------------------------------------------


class TestGetSettingsCache:
    def test_returns_settings_instance(self):
        s = get_settings()
        assert isinstance(s, Settings)

    def test_lru_cache_returns_same_instance(self):
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_cache_clear_returns_fresh_instance(self):
        first = get_settings()
        get_settings.cache_clear()
        second = get_settings()
        assert first is not second

    def test_env_change_after_cache_is_ignored(self, monkeypatch):
        first = get_settings()
        original_url = first.krewhub_url
        # Mutating env after first call should not affect the cached instance.
        monkeypatch.setenv("KREWCLI_KREWHUB_URL", "https://changed.example.com")
        cached = get_settings()
        assert cached is first
        assert cached.krewhub_url == original_url

    def test_module_exposes_get_settings(self):
        assert hasattr(config_module, "get_settings")
        assert config_module.get_settings is get_settings


# ---------------------------------------------------------------------------
# Immutability — model_copy is what cli.join uses to override agent_port
# ---------------------------------------------------------------------------


class TestSettingsImmutability:
    def test_model_copy_returns_new_instance(self):
        s = Settings()
        updated = s.model_copy(update={"agent_port": 1234})
        assert updated is not s
        assert updated.agent_port == 1234
        # Original is untouched.
        assert s.agent_port == 9999

    def test_model_copy_preserves_unrelated_fields(self):
        s = Settings(api_key="orig-key", krewhub_url="https://x")
        updated = s.model_copy(update={"agent_port": 1234})
        assert updated.api_key == "orig-key"
        assert updated.krewhub_url == "https://x"

    def test_unknown_field_raises(self):
        with pytest.raises(Exception):
            Settings(not_a_field="boom")  # type: ignore[call-arg]
