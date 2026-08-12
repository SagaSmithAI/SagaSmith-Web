from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SAGASMITH_",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = "development"
    database_url: str = "sqlite:///./sagasmith-service.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    session_secret: SecretStr = Field(
        default=SecretStr("development-only-session-secret-change-me"),
        min_length=32,
    )
    public_origin: str = "http://127.0.0.1:8080"
    dnd_mcp_url: str = "http://127.0.0.1:8767/mcp"
    dnd_mcp_service_principal: str = "service:sagasmith"
    session_ttl_seconds: int = 60 * 60 * 24 * 30
    secure_cookies: bool = False
    signup_token_quota: int = 1_000_000
    agent_reservation_tokens: int = 32_768
    agent_api_url: str = "http://127.0.0.1:8910"
    agent_api_key: SecretStr = SecretStr("")
    private_storage_dir: str = "./data/private"
    exchange_dir: str = "./data/exchange"
    max_pack_bytes: int = 200 * 1024 * 1024
    bootstrap_admin_email: str = ""
    storage_backend: str = "local"
    object_endpoint: str = "http://127.0.0.1:9000"
    object_bucket: str = "sagasmith-private"
    object_access_key: str = ""
    object_secret_key: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
