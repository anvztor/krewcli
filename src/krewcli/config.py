from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse, urlunparse

from pydantic import model_validator
from pydantic_settings import BaseSettings


_DEFAULT_KREWHUB_URL = "http://127.0.0.1:8420"
_DEFAULT_KREW_AUTH_URL = "http://127.0.0.1:8421"


def _derive_krewhub_from_krewauth(krew_auth_url: str) -> str | None:
    """Return a sensible krewhub URL given a krew_auth URL, or None.

    Convention used by every krew deployment we ship:

        auth.<domain>  ↔  hub.<domain>

    so a user that exports ``KREWCLI_KREW_AUTH_URL=https://auth.cookrew.dev``
    almost certainly means ``KREWCLI_KREWHUB_URL=https://hub.cookrew.dev``.
    Returns ``None`` when the host doesn't start with ``auth.`` (we don't
    want to guess for arbitrary URLs — that would mask configuration
    bugs).
    """
    parsed = urlparse(krew_auth_url)
    host = (parsed.hostname or "").lower()
    if not host.startswith("auth."):
        return None
    new_host = "hub." + host[len("auth."):]
    netloc = new_host
    if parsed.port:
        netloc = f"{new_host}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


class Settings(BaseSettings):
    krewhub_url: str = _DEFAULT_KREWHUB_URL
    krew_auth_url: str = _DEFAULT_KREW_AUTH_URL
    # Track A1 alias mirroring spec naming (krewauth_url == krew_auth_url)
    krewauth_url: str = _DEFAULT_KREW_AUTH_URL
    api_key: str = "dev-api-key"
    agent_port: int = 9999
    agent_host: str = "127.0.0.1"
    heartbeat_interval: int = 15
    task_poll_interval: int = 5
    default_recipe_id: str = ""
    default_cookbook_id: str = ""
    jwt_secret: str = ""
    token_expiry_minutes: int = 30
    hook_listener_port: int = 9998
    verify_ssl: bool = True

    # ERC-8004 on GOAT Testnet3
    erc8004_chain_id: int = 48816
    erc8004_rpc_url: str = "https://rpc.testnet3.goat.network"
    erc8004_identity_registry: str = "0x556089008Fc0a60cD09390Eca93477ca254A5522"
    erc8004_reputation_registry: str = "0xd9140951d8aE6E5F625a02F5908535e16e3af964"

    model_config = {"env_prefix": "KREWCLI_"}

    @model_validator(mode="after")
    def _auto_pair_urls(self) -> "Settings":
        """Auto-derive ``krewhub_url`` from ``krew_auth_url`` when only
        the auth URL was overridden.

        Bug context: a user that exports just
        ``KREWCLI_KREW_AUTH_URL=https://auth.cookrew.dev`` would get
        device-flow auth against prod but the daemon's cookbook /
        recipe / agent-registration calls would silently target the
        ``http://127.0.0.1:8420`` default — local krewhub isn't
        running, so login completes but no agents ever come online.

        Fire only when ``krewhub_url`` is the localhost default AND the
        auth URL was changed away from its default — otherwise we'd
        clobber an explicit ``KREWCLI_KREWHUB_URL`` that happens to
        look like localhost.
        """
        if self.krewhub_url != _DEFAULT_KREWHUB_URL:
            return self
        if self.krew_auth_url == _DEFAULT_KREW_AUTH_URL:
            return self
        derived = _derive_krewhub_from_krewauth(self.krew_auth_url)
        if derived:
            self.krewhub_url = derived
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
