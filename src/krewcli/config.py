from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    krewhub_url: str = "http://127.0.0.1:8420"
    krew_auth_url: str = "http://127.0.0.1:8421"
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

    # ERC-8004 on GOAT Testnet3
    erc8004_chain_id: int = 48816
    erc8004_rpc_url: str = "https://rpc.testnet3.goat.network"
    erc8004_identity_registry: str = "0x556089008Fc0a60cD09390Eca93477ca254A5522"
    erc8004_reputation_registry: str = "0xd9140951d8aE6E5F625a02F5908535e16e3af964"

    model_config = {"env_prefix": "KREWCLI_"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
