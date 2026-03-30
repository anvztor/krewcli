from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    krewhub_url: str = "http://127.0.0.1:8420"
    api_key: str = "dev-api-key"
    agent_port: int = 9999
    agent_host: str = "127.0.0.1"
    heartbeat_interval: int = 15
    default_recipe_id: str = ""

    model_config = {"env_prefix": "KREWCLI_"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
