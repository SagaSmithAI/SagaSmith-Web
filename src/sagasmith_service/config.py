from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderPrice(BaseModel):
    provider: Literal["OpenAICompatProvider"]
    version: str = Field(min_length=1)
    valid_until: datetime
    input_usd_per_million: Decimal = Field(gt=0, allow_inf_nan=False)
    cached_usd_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: Decimal = Field(gt=0, allow_inf_nan=False)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    max_request_bytes: int = Field(ge=1)


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
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    auth_rate_limit: int = Field(default=10, ge=1, le=10_000)
    auth_rate_window_seconds: int = Field(default=60, ge=1, le=86_400)
    agent_rate_limit: int = Field(default=60, ge=1, le=100_000)
    pack_rate_limit: int = Field(default=10, ge=1, le=10_000)
    module_rate_limit: int = Field(default=60, ge=1, le=10_000)
    session_secret: SecretStr = Field(
        default=SecretStr("development-only-session-secret-change-me"),
        min_length=32,
    )
    public_origin: str = "http://127.0.0.1:8080"
    dnd_mcp_url: str = "http://127.0.0.1:8767/mcp"
    coc_mcp_url: str = "http://127.0.0.1:8768/mcp"
    session_ttl_seconds: int = 60 * 60 * 24 * 30
    secure_cookies: bool = False
    signup_token_quota: int = 0
    registration_mode: Literal["open", "invite", "closed"] = "open"
    global_rate_limit: int = Field(default=1000, ge=1, le=100_000)
    readiness_cache_seconds: int = Field(default=10, ge=1, le=300)
    provider_budget_enabled: bool = False
    provider_prices: dict[str, ProviderPrice] = Field(default_factory=dict)
    task_budget_usd: Decimal = Field(default=Decimal("1"), gt=0, allow_inf_nan=False)
    agent_reservation_tokens: int = 32_768
    agent_api_url: str = "http://127.0.0.1:8910"
    agent_api_key: SecretStr = SecretStr("")
    agent_boundary_mode: Literal["legacy", "modern"] = "modern"
    auth_context_secret: SecretStr = SecretStr("development-auth-context-secret-change-me")
    service_internal_url: str = "http://127.0.0.1:8080"
    agent_completion_timeout_seconds: int = Field(default=900, ge=30, le=3600)
    agent_reservation_ttl_seconds: int = Field(default=1200, ge=60, le=7200)
    agent_delegation_ttl_seconds: int = Field(default=600, ge=30, le=900)
    room_turn_worker_poll_seconds: float = Field(default=0.1, gt=0, le=60)
    room_turn_worker_lease_seconds: int = Field(default=60, ge=15, le=900)
    room_turn_worker_concurrency: int = Field(default=4, ge=1, le=64)
    room_turn_per_room_concurrency: int = Field(default=4, ge=1, le=64)
    room_turn_worker_max_attempts: int = Field(default=3, ge=1, le=20)
    room_turn_retry_seconds: int = Field(default=2, ge=0, le=3600)
    room_turn_inline_wait_seconds: float = Field(default=30, ge=0, le=120)
    room_turn_media_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    private_storage_dir: str = "./data/private"
    exchange_dir: str = "./data/exchange"
    max_pack_bytes: int = 200 * 1024 * 1024
    max_pack_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_module_source_bytes: int = 100 * 1024 * 1024
    user_upload_storage_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    module_agent_reservation_tokens: int = 131_072
    module_worker_poll_seconds: float = 1.0
    module_worker_lease_seconds: int = 900
    module_worker_concurrency: int = 2
    module_worker_io_concurrency: int = Field(default=2, ge=1, le=64)
    module_worker_metrics_port: int = Field(default=9101, ge=1024, le=65_535)
    module_run_retry_seconds: int = 15
    bootstrap_admin_email: str = ""
    storage_backend: str = "local"
    object_endpoint: str = "http://127.0.0.1:9000"
    object_bucket: str = "sagasmith-private"
    object_region: str = "us-east-1"
    object_create_bucket: bool = False
    object_access_key: str = ""
    object_secret_key: SecretStr = SecretStr("")
    combat_render_cache_entries: int = Field(default=8, ge=1, le=4096)
    combat_render_cache_max_bytes: int = Field(default=64 * 1024 * 1024, ge=10 * 1024 * 1024)
    combat_render_concurrency: int = Field(default=2, ge=1, le=64)
    combat_render_cache_ttl_seconds: float = Field(default=30, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        if self.agent_reservation_ttl_seconds <= self.agent_completion_timeout_seconds:
            raise ValueError(
                "SAGASMITH_AGENT_RESERVATION_TTL_SECONDS must exceed "
                "SAGASMITH_AGENT_COMPLETION_TIMEOUT_SECONDS"
            )
        if self.env != "production":
            return self
        failures: list[str] = []
        placeholder_markers = ("replace", "change-me", "development-only")

        def is_placeholder(value: str) -> bool:
            lowered = value.casefold()
            return any(marker in lowered for marker in placeholder_markers)

        if not self.database_url.startswith("postgresql+psycopg://") or is_placeholder(
            self.database_url
        ):
            failures.append("SAGASMITH_DATABASE_URL must be a non-placeholder PostgreSQL URL")
        if self.rate_limit_backend != "redis":
            failures.append("SAGASMITH_RATE_LIMIT_BACKEND must be redis")
        if not self.secure_cookies:
            failures.append("SAGASMITH_SECURE_COOKIES must be true")
        if not self.public_origin.startswith("https://"):
            failures.append("SAGASMITH_PUBLIC_ORIGIN must use https")
        if is_placeholder(self.session_secret.get_secret_value()):
            failures.append("SAGASMITH_SESSION_SECRET must be replaced")
        agent_key = self.agent_api_key.get_secret_value()
        if len(agent_key) < 32 or is_placeholder(agent_key):
            failures.append("SAGASMITH_AGENT_API_KEY must be a non-placeholder 32-byte secret")
        auth_context_secret = self.auth_context_secret.get_secret_value()
        if len(auth_context_secret) < 32 or is_placeholder(auth_context_secret):
            failures.append(
                "SAGASMITH_AUTH_CONTEXT_SECRET must be a non-placeholder 32-byte secret"
            )
        if self.storage_backend != "s3":
            failures.append("SAGASMITH_STORAGE_BACKEND must be s3")
        if self.registration_mode == "open":
            failures.append("SAGASMITH_REGISTRATION_MODE must be invite or closed")
        if self.signup_token_quota != 0:
            failures.append("SAGASMITH_SIGNUP_TOKEN_QUOTA must be zero for beta")
        if self.bootstrap_admin_email:
            failures.append("SAGASMITH_BOOTSTRAP_ADMIN_EMAIL must be empty; use the offline CLI")
        if self.object_create_bucket:
            failures.append("production object buckets must be provisioned before startup")
        if not self.provider_budget_enabled or not self.provider_prices:
            failures.append("production requires provider budgets and reviewed versioned prices")
        object_secret = self.object_secret_key.get_secret_value()
        if not self.object_access_key or len(object_secret) < 32 or is_placeholder(object_secret):
            failures.append("private object-store credentials are required")
        if failures:
            raise ValueError("unsafe production configuration: " + "; ".join(failures))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
